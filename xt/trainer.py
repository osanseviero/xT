import logging
import math
import os
import re
import time
from numbers import Number
from pathlib import Path
from typing import Dict

import torch
import torch.distributed
import torch.distributed as dist
from einops import rearrange
from omegaconf import OmegaConf
from timm.utils import AverageMeter
from torch.nn import DataParallel, SyncBatchNorm
from torch.nn.modules.batchnorm import _BatchNorm
from torch.nn.parallel.distributed import DistributedDataParallel
from torch.utils.data import DataLoader
from tqdm import tqdm

import wandb

from .datasets.sampler import DistributedWeightedRandomSampler
from .evaluator import Evaluator
from .losses import build_losses
from .models import build_model
from .optimizer import create_optimizer
from .tiling import build_tiling
from .utils import (
    SmoothedValue,
    count_parameters,
    get_rank,
    get_world_size,
    is_dist_avail_and_initialized,
    is_main_process,
)

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO").upper())
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"

WANDB_OFFLINE = False
if os.environ.get("WANDB_MODE", None) == "offline":
    WANDB_OFFLINE = True


class PytorchTrainer:
    def __init__(
        self,
        config,
        evaluator: Evaluator,
        train_loader: DataLoader,
        val_loader: DataLoader,
        process_group=None,
        mixup_fn=None,
    ) -> None:
        super().__init__()
        self.config = config

        self.pg = process_group

        self.evaluator = evaluator
        self.current_metrics = evaluator.init_metrics()
        self.current_epoch = 0
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.mixup_fn = mixup_fn
        self.wandb_id = None
        self.patch_size = config.model.patch_size
        self.context_patch_len = config.model.context.context_patch_len

        self.model = self._init_model()

        self.losses = build_losses(self.config)

        self._init_amp()

        self.optimizer, self.scheduler = create_optimizer(
            self.config.optimizer,
            self.model,
            len(train_loader),
            self.config.train.epochs,
        )
        self._load_optimizer_from_ckpt()

        self.params = count_parameters(model=self.model)

        if is_main_process():
            # Set up wandb offline sync hook for training on
            # clusters without internet access
            if WANDB_OFFLINE:
                from wandb_osh.hooks import TriggerWandbSyncHook

                self.trigger_sync = TriggerWandbSyncHook()

            if config.data.dataset == "inaturalist":
                # TODO
                project_name = "gigaformer inaturalist"

            wandb_args = dict(
                project=project_name,
                entity="bair-climate-initiative",
                resume="allow",
                name=config.name,
                config=OmegaConf.to_container(config),
                dir=str(Path(config.output_dir)),
            )
            artifact = wandb.Artifact(
                "config_file",
                type="config_file",
            )
            config_dump_path = Path(self.config.output_dir) / "config.yaml"
            with open(config_dump_path, "w") as outfile:
                outfile.write(OmegaConf.to_yaml(self.config))
            artifact.add_file(config_dump_path)
            wandb.init(**wandb_args)
            wandb.log_artifact(artifact)
            self.wandb_id = wandb.run.id
            wandb.run.summary["total_params"] = self.params
            if os.environ.get("SLURM_JOBID", None):
                wandb.config.update({"SLURM_JOBID": os.environ["SLURM_JOBID"]})

            print(self.model)

    def validate(self, test_loader=None):
        if is_dist_avail_and_initialized():
            dist.barrier()
        self.model.eval()
        metrics = self.evaluator.validate(
            test_loader if test_loader is not None else self.val_loader,
            self.model,
            distributed=is_dist_avail_and_initialized(),
            local_rank=get_rank(),
            snapshot_name=self.snapshot_name,
        )
        if is_main_process():
            print(metrics)
        if is_main_process() and wandb.run is not None:
            wandb.log(metrics)
            if WANDB_OFFLINE:
                self.trigger_sync()

    def fit(self):
        for epoch in range(self.current_epoch, self.config.train.epochs):
            rank = get_rank()
            logging.debug(f"{rank}: epoch start")
            if self.config.distributed:
                dist.barrier()
            self.current_epoch = epoch
            logging.debug(f"{rank}: set train mode")
            self.model.train()
            self._freeze()
            self._run_one_epoch_train()
            torch.cuda.synchronize()
            if self.config.distributed:
                dist.barrier()
            self.model.eval()
            self._save_last()
            logging.debug(f"{rank} Epoch finished")
            if (self.current_epoch + 1) % self.config.train.test_every == 0:
                logging.debug(f"{rank} eval launched")
                metrics = self.evaluator.validate(
                    self.val_loader,
                    self.model,
                )
                logging.debug(f"{rank} eval done")
                if is_main_process() and wandb.run is not None:
                    metrics["epoch"] = epoch
                    wandb.log(metrics)
                    if WANDB_OFFLINE:
                        self.trigger_sync()
                improved_metrics = None
                if is_main_process():
                    improved_metrics = self.evaluator.get_improved_metrics(
                        prev_metrics=self.current_metrics, current_metrics=metrics
                    )
                    self.current_metrics.update(improved_metrics)
                self._save_best(improved_metrics)


    def _get_current_payload(self):
        payload = {
            "epoch": self.current_epoch,
            "state_dict": self.model.state_dict(),  # ? need .cpu()?
            "optimizer_state_dict": self.optimizer.state_dict(),
            "metrics": self.current_metrics,
        }

        return payload

    def _save_last(self):
        payload = self._get_current_payload()
        checkpoint_dir = Path(self.config.output_dir) / "checkpoints"
        if is_main_process():
            torch.save(
                payload,
                checkpoint_dir
                / f"{self.snapshot_name}_{str(self.wandb_id)}_{self.current_epoch}.ckpt",
            )

    def _save_best(self, improved_metrics: Dict):
        payload = self._get_current_payload()
        checkpoint_dir = Path(self.config.output_dir) / "checkpoints"
        if is_main_process():
            for metric_name in improved_metrics.keys():
                torch.save(
                    payload,
                    checkpoint_dir
                    / f"{self.snapshot_name}_{str(self.wandb_id)}_{metric_name}.ckpt",
                )

    def build_iterator(self, dataloader):
        for x in dataloader:
            old_dim = x["image"].shape[-1]
            n = old_dim // self.input_size
            rearranged_image = rearrange(
                x["image"],
                "N C (H PH GH) (W PW GW )-> N C H W  PH PW GH GW",
                PH=self.input_size // self.patch_size,
                PW=self.input_size // self.patch_size,
                GH=self.patch_size,
                GW=self.patch_size,
            )
            N, C, H, W, PH, PW, PPH, PPW = rearranged_image.shape
            rearranged_image = rearranged_image.flatten(2, 5)
            for i, j, k in build_tiling(n, self.config.model.context.tiling):
                indices = torch.rand(N, H, W, PH, PW)
                indices[:, i, j] = 999
                indices = indices.flatten(1).argsort(-1)
                indices = indices[:, : self.context_patch_len]
                context_patches = torch.stack(
                    [rearranged_image[i][:, v] for i, v in enumerate(indices)],
                    dim=0,
                )  # N C L 16 16
                H_i = indices // (W * PH * PW)
                W_i = (indices // (PH * PW)) % W
                PH_i = (indices // (PW)) % PH
                PW_i = indices % PW
                # assert torch.all(indices == H_i * (W * PH*PW) + W_i *PH*PW + PH_i * PW + PW_i) sanity check
                h_idx = H_i * PH + PH_i
                w_idx = W_i * PW + PW_i

                raw_indices_h = torch.arange(PH) + i * PH
                raw_indices_w = torch.arange(PH) + j * PW
                raw_indices = torch.stack(
                    [
                        raw_indices_h[:, None].repeat(1, PW),
                        raw_indices_w[None,].repeat(PH, 1),
                    ]
                )
                patch_indices = torch.stack([h_idx, w_idx])  # 2 X B X L
                new_payload = {}
                for key, v in x.items():
                    if torch.is_tensor(v) and len(v.shape) >= 2:
                        new_payload[key] = v[
                            ...,
                            self.input_size * i : self.input_size * (i + 1),
                            self.input_size * j : self.input_size * (j + 1),
                        ]
                        # print(new_payload[key].shape)
                    else:
                        new_payload[key] = v
                new_payload["context_patches"] = context_patches
                new_payload["patch_indices"] = patch_indices
                new_payload["raw_indices"] = raw_indices
                new_payload["context_id"] = k["context_id"]
                new_payload["mem_only"] = k.get("mem_only", False)
                new_payload["cord"] = (i, j)
                yield new_payload

    def _run_one_epoch_train(self):
        torch.autograd.set_detect_anomaly(True)
        train_loader = self.train_loader
        len_train_loader = len(self.train_loader)

        loss_meter = AverageMeter()
        avg_meters = {"loss": loss_meter}
        for loss_def in self.losses:
            if loss_def.display:
                avg_meters[loss_def.name] = AverageMeter()
        data_time = SmoothedValue(fmt="{avg:.4f}")

        if self.config.optimizer.mode == "epoch":
            self.scheduler.step(self.current_epoch)
        iter_scale = 1

        if is_main_process():
            # Only show the progress bar on main process
            train_loader_tqdm = tqdm(train_loader, total=iter_scale * len_train_loader)
        train_loader = iter(train_loader)

        for i in range(iter_scale * len_train_loader):
            start_time = time.time()
            sample = next(train_loader)
            data_time.update(time.time() - start_time)

            imgs = sample["image"].cuda()
            labels = sample["label"].cuda() if "label" in sample else None

            if self.mixup_fn is not None:
                imgs, sample["label"] = self.mixup_fn(imgs, labels)

            self.optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=self.config.fp16):
                with torch.autograd.detect_anomaly():
                    output = self.model(imgs)
                    total_loss = 0
                    for loss_def in self.losses:
                        loss = loss_def.loss.calculate_loss(output, sample)
                        if math.isnan(loss.item()) or math.isinf(loss.item()):
                            print(loss_def)
                            print("is nan!")
                        if loss_def.display:
                            avg_meters[loss_def.name].update(
                                loss if isinstance(loss, Number) else loss.item(),
                                imgs.size(0),
                            )
                        total_loss += loss_def.weight * loss

            loss_meter.update(total_loss.item(), imgs.size(0))

            if math.isnan(total_loss.item()) or math.isinf(total_loss.item()):
                raise ValueError("NaN loss !!")

            avg_metrics = {k: f"{v.avg:.4f}" for k, v in avg_meters.items()}
            if is_main_process() and wandb.run is not None and i % 50 == 0:
                # Log metrics to wandb
                payload = {k: float(f"{v.avg:.4f}") for k, v in avg_meters.items()}
                payload.update(dict(lr=float(self.scheduler.get_lr()[-1])))
                payload.update({"epoch": self.current_epoch})
                wandb.log(payload)
                if WANDB_OFFLINE:
                    self.trigger_sync()

            # Run backward pass
            total_loss.backward()
            torch.nn.utils.clip_grad_norm_(
                self.model.parameters(), self.config.train.clip_grad
            )
            self.optimizer.step()

            torch.cuda.synchronize()
            if is_dist_avail_and_initialized():
                dist.barrier()

            if self.config.optimizer.mode in ("step", "poly"):
                self.scheduler.step(
                    int(i / iter_scale) + self.current_epoch * len_train_loader
                )

            if is_main_process():
                train_loader_tqdm.set_postfix(
                    {
                        "lr": float(self.scheduler.get_lr()[-1]),
                        "epoch": self.current_epoch,
                        "mem": f"{torch.cuda.max_memory_reserved() / 1024 ** 3:.2f}G",
                        **avg_metrics,
                        "data": data_time,
                    }
                )
                train_loader_tqdm.update()

    @property
    def train_batch_size(self):
        return self.config.train.batch_size

    @property
    def val_batch_size(self):
        return self.config.train.val_batch_size

    def get_train_loader(self) -> DataLoader:
        if is_dist_avail_and_initialized():
            train_sampler = torch.utils.data.distributed.DistributedSampler(
                self.train_data
            )
            if hasattr(self.train_data, "get_weights"):
                train_sampler = DistributedWeightedRandomSampler(
                    self.train_data, self.train_data.get_weights()
                )
            train_sampler.set_epoch(self.current_epoch)
        train_data_loader = DataLoader(
            self.train_data,
            batch_size=self.config.train.batch_size,
            num_workers=self.config.data.num_workers,
            shuffle=train_sampler is None,
            sampler=train_sampler,
            pin_memory=False,
            drop_last=True,
        )
        return train_data_loader

    def get_val_loader(self) -> DataLoader:
        if is_dist_avail_and_initialized():
            val_sampler = torch.utils.data.distributed.DistributedSampler(
                self.val_data,
                shuffle=False,
                num_replicas=get_world_size(),
                rank=get_rank(),
            )
        val_data_loader = DataLoader(
            self.val_data,
            sampler=val_sampler,
            batch_size=self.config.train.val_batch_size,
            num_workers=self.config.data.num_workers,
            shuffle=False,
            pin_memory=False,
        )
        # self.val_sampler = val_sampler
        # self.val_data_loader = val_data_loader
        return val_data_loader

    @property
    def snapshot_name(self):
        return f"{self.config.model.name}_{self.config.model.backbone_class}"

    def _freeze(self):
        if hasattr(self.model.module, "encoder"):
            encoder = self.model.module.encoder
        elif hasattr(self.model.module, "encoder_stages"):
            encoder = self.model.module.encoder_stages
        else:
            logging.warn("unknown encoder model")
            return
        if self.current_epoch < self.config.train.freeze_epochs:
            encoder.eval()
            for p in encoder.parameters():
                p.requires_grad = False
        else:
            encoder.train()
            for p in encoder.parameters():
                p.requires_grad = True
        if self.config.train.freeze_bn:
            for m in self.model.modules():
                if isinstance(m, _BatchNorm):
                    m.eval()
                    for p in m.parameters():
                        p.requires_grad = False

    def _init_amp(self):
        self.gscaler = torch.cuda.amp.GradScaler()

        if self.config.distributed and self.config.fsdp:
            from timm.models.swin_transformer_v2 import SwinTransformerV2Block
            from torch.distributed.fsdp import (
                CPUOffload,
                FullyShardedDataParallel,
                MixedPrecision,
            )
            from torch.distributed.fsdp.wrap import ModuleWrapPolicy

            fpSixteen = MixedPrecision(
                param_dtype=torch.float32,
                # Gradient communication precision.
                reduce_dtype=torch.float16,
                # Buffer precision.
                buffer_dtype=torch.float16,
            )

            self.model = FullyShardedDataParallel(
                self.model,
                auto_wrap_policy=ModuleWrapPolicy(
                    module_classes=[SwinTransformerV2Block]
                ),
                cpu_offload=CPUOffload(offload_params=True),
                mixed_precision=fpSixteen,
            )
        elif self.config.distributed:
            self.model = DistributedDataParallel(
                self.model,
                device_ids=[get_rank()],
                output_device=get_rank(),
            )
        else:
            self.model = DataParallel(self.model).cuda()

    def _load_checkpoint(self, model: torch.nn.Module):
        checkpoint_path = self.config.model.resume
        if not checkpoint_path:
            return
        if os.path.isfile(checkpoint_path):
            if is_main_process():
                print("=> loading checkpoint '{}'".format(checkpoint_path))
            checkpoint = torch.load(checkpoint_path, map_location="cpu")
            if "state_dict" in checkpoint:
                state_dict = checkpoint["state_dict"]
                state_dict = {
                    re.sub("^module.", "", k): w for k, w in state_dict.items()
                }
                orig_state_dict = model.state_dict()
                mismatched_keys = []
                for k, v in state_dict.items():
                    ori_size = (
                        orig_state_dict[k].size() if k in orig_state_dict else None
                    )
                    if v.size() != ori_size:
                        print(
                            "SKIPPING!!! Shape of {} changed from {} to {}".format(
                                k, v.size(), ori_size
                            )
                        )
                        mismatched_keys.append(k)
                for k in mismatched_keys:
                    del state_dict[k]
                model.load_state_dict(state_dict, strict=False)
                self.current_epoch = checkpoint["epoch"]
                self.current_metrics = checkpoint.get(
                    "metrics", self.evaluator.init_metrics()
                )
                if is_main_process():
                    print(
                        "=> loaded checkpoint '{}' (epoch {})".format(
                            checkpoint_path, checkpoint["epoch"]
                        )
                    )
            else:
                model.load_state_dict(checkpoint)
        else:
            if is_main_process():
                print("=> no checkpoint found at '{}'".format(checkpoint_path))

    def _load_optimizer_from_ckpt(self):
        checkpoint_path = self.config.model.resume
        if not checkpoint_path:
            return
        if os.path.isfile(checkpoint_path):
            checkpoint = torch.load(checkpoint_path, map_location="cpu")
            if "optimizer_state_dict" in checkpoint:
                self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
                if is_main_process():
                    print("=> loaded optimizer state")
            else:
                if is_main_process():
                    print(
                        "=> no optimizer checkpoint found at '{}'".format(
                            checkpoint_path
                        )
                    )

    def _init_model(self):
        self.input_size = self.config.model.backbone.img_size
        model = build_model(self.config.model, self.config.data.dataset)

        model = model.cuda()
        self._load_checkpoint(model)

        if self.config.distributed and not self.config.train.freeze_bn:
            model = SyncBatchNorm.convert_sync_batchnorm(model, self.pg)
        channels_last = self.config.model.backbone.channel_last
        if channels_last:
            model = model.to(memory_format=torch.channels_last)
        return model
