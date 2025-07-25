# Copyright (c) Adobe EntitySAM team.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import copy
import itertools
import logging
import os

from collections import OrderedDict
from typing import Any, Dict, List, Set

import torch
import weakref

import detectron2.utils.comm as comm
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.config import get_cfg
from detectron2.data import MetadataCatalog, build_detection_train_loader
from detectron2.engine import (
    DefaultTrainer,
    default_argument_parser,
    default_setup,
    launch,
    create_ddp_model,
    AMPTrainer,
    SimpleTrainer,
    HookBase,
)
from detectron2.evaluation import (
    COCOEvaluator,
    COCOPanopticEvaluator,
    SemSegEvaluator,
    DatasetEvaluator,
    LVISEvaluator,
    inference_on_dataset,
    print_csv_format,
    verify_results,
)

from detectron2.projects.deeplab import add_deeplab_config, build_lr_scheduler
from detectron2.solver.build import maybe_add_gradient_clipping
from detectron2.utils.logger import setup_logger

from sam2.build_sam import build_sam2_video_query_iou_predictor
from train import (
    UniVidDatasetMapper,
    OpenVocabularyCocoPanoClipDatasetMapper,
    EntitySegClipDatasetMapper,
    build_combined_loader,
    build_detection_train_loader,
    build_detection_test_loader,
    add_train_config
)



class Trainer(DefaultTrainer):
    """
    Extension of the Trainer class adapted to MaskFormer.
    """
    def __init__(self, cfg):
        """
        Args:
            cfg (CfgNode):
        """

        logger = logging.getLogger("detectron2")
        if not logger.isEnabledFor(logging.INFO):  # setup_logger is not called for d2
            setup_logger()
        cfg = DefaultTrainer.auto_scale_workers(cfg, comm.get_world_size())

        # Assume these objects must be constructed in this order.
        model = self.build_model(cfg)
        optimizer = self.build_optimizer(cfg, model)
        data_loader = self.build_train_loader(cfg)

        model = create_ddp_model(model, broadcast_buffers=False, find_unused_parameters=True)
        self._trainer = (AMPTrainer if cfg.SOLVER.AMP.ENABLED else SimpleTrainer)(
            model, data_loader, optimizer
        )

        self.scheduler = self.build_lr_scheduler(cfg, optimizer)
        self.checkpointer = DetectionCheckpointer(
            # Assume you want to save checkpoints together with logs/statistics
            model,
            cfg.OUTPUT_DIR,
            trainer=weakref.proxy(self),
        )

        self.start_iter = 0
        self.max_iter = cfg.SOLVER.MAX_ITER
        self.cfg = cfg

        self._hooks: List[HookBase] = []
        self.register_hooks(self.build_hooks())

    @classmethod
    def build_model(cls, cfg):
        """
        Override the build_model method to instantiate the model directly
        without using Detectron2's registry.
        """
        if cfg.MODEL.NAME == 'vitl':
            sam2_checkpoint = "./checkpoints/sam2.1_hiera_large.pt"
            model_cfg = "configs/sam2.1_hiera_l.yaml"
        elif cfg.MODEL.NAME == 'vits':
            sam2_checkpoint = "./checkpoints/sam2.1_hiera_small.pt"
            model_cfg = "configs/sam2.1_hiera_s.yaml"
        else:
            raise ValueError(f"Invalid model name: {cfg.MODEL.NAME}")

        model = build_sam2_video_query_iou_predictor(model_cfg, sam2_checkpoint,mode='train',apply_postprocessing=False, mask_decoder_depth=cfg.MODEL.MASK_DECODER_DEPTH)
        print("MODEL_NAME:  ",cfg.MODEL.NAME)


        tune_name_list = [
            'sam_mask_decoder.transformer',
            'sam_mask_decoder.queries',
            'sam_mask_decoder.iou_queries',
            'sam_mask_decoder.output_upscaling',
            'sam_mask_decoder.conv_s0',
            'sam_mask_decoder.conv_s1',
            'sam_mask_decoder.output_hypernetworks_mlps.0',
            "sam_mask_decoder.cls_prediction_head",
            "sam_mask_decoder.iou_prediction_head",
            "sam_mask_decoder.level_embed",
            "sam_mask_decoder.mask_embed",
            "point_sampler_net",
            "point_sampler_num",
            "offset_attention",
            "offset_embedding",
            "offset_fc",
            "offset_cls",
            "dinov2_projector",
            "backbone_feature_enhancement"
        ]
        for n, p in model.named_parameters():
            if any(n.startswith(prefix) for prefix in tune_name_list):
                print('Tuning decoder parameters:   ', n)
                p.requires_grad = True
            else:
                p.requires_grad = False
                if 'sam_mask_decoder' in n:
                    print('Freeze decoder parameters: ', n)
        
        logger = logging.getLogger(__name__)
        logger.info("Model:\n{}".format(model))
        print(model)
        
        return model


    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        """
        Create evaluator(s) for a given dataset.
        This uses the special metadata "evaluator_type" associated with each
        builtin dataset. For your own dataset, you can simply create an
        evaluator manually in your script and do not have to worry about the
        hacky if-else logic here.
        """
        
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
            os.makedirs(output_folder, exist_ok=True)
    
        evaluator_list = []
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type
        if evaluator_type in {"imagenet"}:
            evaluator_list.append(ImageNetEvaluator(dataset_name, output_folder))
        elif evaluator_type in {"coco_panoptic_seg", "ade20k_panoptic_seg"}:
            if cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON:
                evaluator_list.append(COCOPanopticEvaluator(dataset_name, output_folder))
            elif cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON:
                evaluator_list.append(SemSegEvaluator(dataset_name, True, output_folder))
            elif cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON:
                evaluator_list.append(COCOEvaluator(dataset_name, cfg, True, output_folder))
            else:
                raise NotImplementedError('Not support the evaluator type.')
        elif evaluator_type in {"coco"}:
            evaluator_list.append(COCOEvaluator(dataset_name, cfg, True, output_folder))
        else:
            raise NotImplementedError('Not support the evaluator type {}'.format(dataset_name))

        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        elif len(evaluator_list) == 1:
            return evaluator_list[0]
        else:
            raise NotImplementedError

    @classmethod
    def build_train_loader(cls, cfg):
        mappers = []
        for d_i, dataset_name in enumerate(cfg.DATASETS.TRAIN):
            if (dataset_name.startswith('coco') and 'panoptic' not in dataset_name) or dataset_name.startswith('sa_1b'):
                mappers.append(
                    CocoClipDatasetMapper(cfg, is_train=True, dataset_name=dataset_name)
                )
            elif dataset_name.startswith('coco') and 'panoptic' in dataset_name:
                mappers.append(
                    OpenVocabularyCocoPanoClipDatasetMapper(cfg, is_train=True, is_tgt=True, src_dataset_name=dataset_name, )
                )
            elif dataset_name.startswith('entityseg'):
                mappers.append(
                    EntitySegClipDatasetMapper(cfg, is_train=True, dataset_name=dataset_name, )
                )
            else:
                mappers.append(
                    UniVidDatasetMapper(cfg, is_train=True, dataset_name=dataset_name)
                )
            
        assert len(mappers) > 0, "No dataset is chosen!"

        if len(mappers) == 1:
            mapper = mappers[0]
            return build_detection_train_loader(cfg, mapper=mapper, dataset_name=cfg.DATASETS.TRAIN[0])
        else:
            loaders = [
                build_detection_train_loader(cfg, mapper=mapper, dataset_name=dataset_name)
                for mapper, dataset_name in zip(mappers, cfg.DATASETS.TRAIN)
            ]
            combined_data_loader = build_combined_loader(cfg, loaders, cfg.DATASETS.DATASET_RATIO)
            return combined_data_loader

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        if dataset_name.startswith('coco') or dataset_name.startswith("refcoco") \
                or dataset_name.startswith("lvis"):
            mapper = CocoClipDatasetMapper(cfg, is_train=False, dataset_name=dataset_name)
            return build_detection_test_loader(cfg, dataset_name, mapper=mapper)
        else:
            mapper = UniVidDatasetMapper(cfg, is_train=False, dataset_name=dataset_name)
            return build_detection_test_loader(cfg, dataset_name, mapper=mapper)

    @classmethod
    def build_lr_scheduler(cls, cfg, optimizer):
        """
        It now calls :func:`detectron2.solver.build_lr_scheduler`.
        Overwrite it if you'd like a different scheduler.
        """
        return build_lr_scheduler(cfg, optimizer)

    @classmethod
    def build_optimizer(cls, cfg, model):
        weight_decay_norm = cfg.SOLVER.WEIGHT_DECAY_NORM
        weight_decay_embed = cfg.SOLVER.WEIGHT_DECAY_EMBED

        defaults = {}
        defaults["lr"] = cfg.SOLVER.BASE_LR
        defaults["weight_decay"] = cfg.SOLVER.WEIGHT_DECAY

        norm_module_types = (
            torch.nn.BatchNorm1d,
            torch.nn.BatchNorm2d,
            torch.nn.BatchNorm3d,
            torch.nn.SyncBatchNorm,
            # NaiveSyncBatchNorm inherits from BatchNorm2d
            torch.nn.GroupNorm,
            torch.nn.InstanceNorm1d,
            torch.nn.InstanceNorm2d,
            torch.nn.InstanceNorm3d,
            torch.nn.LayerNorm,
            torch.nn.LocalResponseNorm,
        )

        params: List[Dict[str, Any]] = []
        memo: Set[torch.nn.parameter.Parameter] = set()
        for module_name, module in model.named_modules():
            for module_param_name, value in module.named_parameters(recurse=False):
                if not value.requires_grad:
                    continue
                # Avoid duplicating parameters
                if value in memo:
                    continue
                memo.add(value)

                hyperparams = copy.copy(defaults)
                if "image_encoder" in module_name:
                    if 'ctm' not in module_name:
                        hyperparams["lr"] = hyperparams["lr"] * cfg.SOLVER.BACKBONE_MULTIPLIER
                        print("image_encoder learning rate :    ", hyperparams["lr"] * cfg.SOLVER.BACKBONE_MULTIPLIER )

                if (
                    "relative_position_bias_table" in module_param_name
                    or "absolute_pos_embed" in module_param_name
                ):
                    print(module_param_name)
                    hyperparams["weight_decay"] = 0.0
                if isinstance(module, norm_module_types):
                    hyperparams["weight_decay"] = weight_decay_norm
                if isinstance(module, torch.nn.Embedding):
                    hyperparams["weight_decay"] = weight_decay_embed
                params.append({"params": [value], **hyperparams})

        def maybe_add_full_model_gradient_clipping(optim):
            # detectron2 doesn't have full model gradient clipping now
            clip_norm_val = cfg.SOLVER.CLIP_GRADIENTS.CLIP_VALUE
            enable = (
                cfg.SOLVER.CLIP_GRADIENTS.ENABLED
                and cfg.SOLVER.CLIP_GRADIENTS.CLIP_TYPE == "full_model"
                and clip_norm_val > 0.0
            )

            class FullModelGradientClippingOptimizer(optim):
                def step(self, closure=None):
                    all_params = itertools.chain(*[x["params"] for x in self.param_groups])
                    torch.nn.utils.clip_grad_norm_(all_params, clip_norm_val)
                    super().step(closure=closure)

            return FullModelGradientClippingOptimizer if enable else optim

        optimizer_type = cfg.SOLVER.OPTIMIZER
        if optimizer_type == "SGD":
            optimizer = maybe_add_full_model_gradient_clipping(torch.optim.SGD)(
                params, cfg.SOLVER.BASE_LR, momentum=cfg.SOLVER.MOMENTUM
            )
        elif optimizer_type == "ADAMW":
            optimizer = maybe_add_full_model_gradient_clipping(torch.optim.AdamW)(
                params, cfg.SOLVER.BASE_LR
            )
        else:
            raise NotImplementedError(f"no optimizer type {optimizer_type}")
        if not cfg.SOLVER.CLIP_GRADIENTS.CLIP_TYPE == "full_model":
            optimizer = maybe_add_gradient_clipping(cfg, optimizer)
        return optimizer

    @classmethod
    def test(cls, cfg, model, evaluators=None):
        """
        Evaluate the given model. The given model is expected to already contain
        weights to evaluate.
        Args:
            cfg (CfgNode):
            model (nn.Module):
            evaluators (list[DatasetEvaluator] or None): if None, will call
                :meth:`build_evaluator`. Otherwise, must have the same length as
                ``cfg.DATASETS.TEST``.
        Returns:
            dict: a dict of result metrics
        """
        from torch.cuda.amp import autocast
        logger = logging.getLogger(__name__)
        if isinstance(evaluators, DatasetEvaluator):
            evaluators = [evaluators]
        if evaluators is not None:
            assert len(cfg.DATASETS.TEST) == len(evaluators), "{} != {}".format(
                len(cfg.DATASETS.TEST), len(evaluators)
            )

        results = OrderedDict()
        for idx, dataset_name in enumerate(cfg.DATASETS.TEST):
            data_loader = cls.build_test_loader(cfg, dataset_name)
            # When evaluators are passed in as arguments,
            # implicitly assume that evaluators can be created before data_loader.
            if evaluators is not None:
                evaluator = evaluators[idx]
            else:
                try:
                    evaluator = cls.build_evaluator(cfg, dataset_name)
                except NotImplementedError:
                    logger.warning(
                        "No evaluator found. Use `DefaultTrainer.test(evaluators=)`, "
                        "or implement its `build_evaluator` method."
                    )
                    results[dataset_name] = {}
                    continue
            with autocast():
                results_i = inference_on_dataset(model, data_loader, evaluator)
            results[dataset_name] = results_i
            if comm.is_main_process():
                assert isinstance(
                    results_i, dict
                ), "Evaluator must return a dict on the main process. Got {} instead.".format(
                    results_i
                )
                logger.info("Evaluation results for {} in csv format:".format(dataset_name))
                print_csv_format(results_i)

        if len(results) == 1:
            results = list(results.values())[0]
        return results


def setup(args):
    """
    Create configs and perform basic setups.
    """
    cfg = get_cfg()

    add_deeplab_config(cfg)  # for poly lr schedule
    add_train_config(cfg)
    # cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()
    default_setup(cfg, args)
    # Setup logger for "mask_former" module
    setup_logger(output=cfg.OUTPUT_DIR, distributed_rank=comm.get_rank(), name="sam2_everything")

    return cfg


def main(args):
    local_rank = comm.get_local_rank()
    torch.cuda.set_device(local_rank)
    print(f"Process {comm.get_rank()} using GPU {local_rank}")
    device = torch.device("cuda", local_rank)
    print(f"Process {comm.get_rank()} using device {device}")
    cfg = setup(args)

    if args.eval_only:
        model = Trainer.build_model(cfg)
        DetectionCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
            cfg.MODEL.WEIGHTS, resume=args.resume
        )
        res = Trainer.test(cfg, model)
        if cfg.TEST.AUG.ENABLED:
            raise NotImplementedError
        if comm.is_main_process():
            verify_results(cfg, res)
        return res

    trainer = Trainer(cfg)
    trainer.resume_or_load(resume=args.resume)
    return trainer.train()


if __name__ == "__main__":
    torch.hub.set_dir(f"/tmp/torch_hub_cache_{os.getpid()}")
    
    args = default_argument_parser().parse_args()
    print("Command Line Args:", args)
    launch(
        main,
        args.num_gpus,
        num_machines=args.num_machines,
        machine_rank=args.machine_rank,
        dist_url=args.dist_url,
        args=(args,),
    )
