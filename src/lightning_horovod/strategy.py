# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
from contextlib import ExitStack
from typing import Any, Dict, List, Optional, Tuple, Union

import horovod.torch as hvd
import torch
import torch.nn as nn
from lightning_utilities import module_available
from torch import Tensor
from torch.optim import Optimizer

if module_available("lightning"):
    from lightning.fabric.plugins import CheckpointIO
    from lightning.fabric.utilities.distributed import _distributed_available
    from lightning.fabric.utilities.distributed import group as dist_group
    from lightning.fabric.utilities.types import ReduceOp
    from lightning.pytorch import Trainer
    from lightning.pytorch.accelerators import Accelerator
    from lightning.pytorch.core.optimizer import LightningOptimizer
    from lightning.pytorch.plugins.precision import PrecisionPlugin
    from lightning.pytorch.strategies.parallel import ParallelStrategy
    from lightning.pytorch.strategies.strategy import TBroadcast
    from lightning.pytorch.utilities.exceptions import MisconfigurationException
    from lightning.pytorch.utilities.rank_zero import rank_zero_only
elif module_available("pytorch_lightning") and module_available("lightning_fabric"):
    from lightning_fabric.plugins import CheckpointIO
    from lightning_fabric.utilities.distributed import _distributed_available
    from lightning_fabric.utilities.distributed import group as dist_group
    from lightning_fabric.utilities.types import ReduceOp
    from pytorch_lightning import Trainer
    from pytorch_lightning.accelerators import Accelerator
    from pytorch_lightning.core.optimizer import LightningOptimizer
    from pytorch_lightning.plugins.precision import PrecisionPlugin
    from pytorch_lightning.strategies.parallel import ParallelStrategy
    from pytorch_lightning.strategies.strategy import TBroadcast
    from pytorch_lightning.utilities.exceptions import MisconfigurationException
    from pytorch_lightning.utilities.rank_zero import rank_zero_only
else:
    raise ModuleNotFoundError("You are missing `lightning` or `pytorch-lightning` package, please install it.")

_HOROVOD_NCCL_AVAILABLE = False

with contextlib.suppress(AttributeError):
    # `nccl_built` returns an integer
    _HOROVOD_NCCL_AVAILABLE = bool(hvd.nccl_built())
    # AttributeError can be raised if MPI is not available:
    # https://github.com/horovod/horovod/blob/v0.23.0/horovod/torch/__init__.py#L33-L34


class HorovodStrategy(ParallelStrategy):
    """Plugin for Horovod distributed training integration."""

    strategy_name = "horovod"
    optimizers: List[Optimizer]

    def __init__(
        self,
        accelerator: Optional[Accelerator] = None,
        parallel_devices: Optional[List[torch.device]] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision_plugin: Optional[PrecisionPlugin] = None,
    ):
        hvd.init()
        super().__init__(
            accelerator=accelerator,
            parallel_devices=parallel_devices,
            cluster_environment=None,
            checkpoint_io=checkpoint_io,
            precision_plugin=precision_plugin,
        )
        rank_zero_only.rank = self.global_rank
        self._exit_stack: Optional[ExitStack] = None

    @property
    def global_rank(self) -> int:
        return hvd.rank()

    @property
    def local_rank(self) -> int:
        return hvd.local_rank()

    @property
    def world_size(self) -> int:
        return hvd.size()

    @property
    def root_device(self) -> torch.device:
        assert isinstance(self.parallel_devices, list)
        return self.parallel_devices[self.local_rank]

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, Any]:
        return {"num_replicas": self.world_size, "rank": self.global_rank}

    @property
    def handles_gradient_accumulation(self) -> bool:
        """Whether the plugin handles gradient accumulation internally."""
        return True

    def setup(self, trainer: Trainer) -> None:
        self.model_to_device()

        super().setup(trainer)

        self._exit_stack = ExitStack()
        self._exit_stack.__enter__()

        if not trainer.training:
            # no need to setup optimizers
            return

        def _unpack_lightning_optimizer(opt: Optimizer) -> Optimizer:
            return opt._optimizer if isinstance(opt, LightningOptimizer) else opt

        optimizers = self.optimizers
        optimizers = [_unpack_lightning_optimizer(opt) for opt in optimizers]

        # Horovod: scale the learning rate by the number of workers to account for
        # increased total batch size
        for optimizer in optimizers:
            for param_group in optimizer.param_groups:
                param_group["lr"] *= self.world_size

        # Horovod: adjust base LR used by schedulers to match scaled optimizer initial LR
        lr_scheduler_configs = self.lr_scheduler_configs
        for config in lr_scheduler_configs:
            scheduler = config.scheduler
            if hasattr(scheduler, "base_lrs"):
                scheduler.base_lrs = [lr * self.world_size for lr in scheduler.base_lrs]

        assert self.lightning_module is not None
        # Horovod: broadcast parameters & optimizer state to ensure consistent initialization
        hvd.broadcast_parameters(self.lightning_module.state_dict(), root_rank=0)
        for optimizer in optimizers:
            hvd.broadcast_optimizer_state(optimizer, root_rank=0)

        self.optimizers = self._wrap_optimizers(optimizers, trainer.accumulate_grad_batches)
        for optimizer in self.optimizers:
            # Synchronization will be performed explicitly following backward()
            self._exit_stack.enter_context(optimizer.skip_synchronize())

    def barrier(self, *args: Any, **kwargs: Any) -> None:
        if _distributed_available():
            self.join()

    def broadcast(self, obj: TBroadcast, src: int = 0) -> TBroadcast:
        return hvd.broadcast_object(obj, src)

    def model_to_device(self) -> None:
        if self.root_device.type == "cuda":
            # this can potentially be removed after #8312. Not done due to lack of horovod testing
            torch.cuda.set_device(self.root_device)
        assert self.model is not None
        self.model.to(self.root_device)

    def join(self) -> None:
        if self.root_device.type == "cuda":
            hvd.join(self.local_rank)
        else:
            hvd.join()

    def reduce(
        self,
        tensor: Union[Any, Tensor],
        group: Optional[Any] = None,
        reduce_op: Optional[Union[ReduceOp, str]] = "mean",
    ) -> Union[Any, Tensor]:
        """Reduces a tensor from several distributed processes to one aggregated tensor.

        Args:
            tensor: the tensor to sync and reduce
            group: the process group to gather results from. Defaults to all processes (world)
            reduce_op: the reduction operation. Defaults to 'mean'/'avg'.
                Can also be a string 'sum' to calculate the sum during reduction.

        Return:
            reduced value, except when the input was not a tensor the output remains is unchanged
        """
        if group is not None:
            raise ValueError("Horovod does not support allreduce using a subcommunicator at this time. Unset `group`.")

        if reduce_op in (None, "avg", "mean"):
            reduce_op = hvd.Average
        elif reduce_op in ("sum", ReduceOp.SUM):
            reduce_op = hvd.Sum
        else:
            raise ValueError(f"unrecognized `reduce_op`: {reduce_op}")

        # sync all processes before reduction
        self.join()
        return hvd.allreduce(tensor, op=reduce_op)

    def all_gather(self, result: Tensor, group: Optional[Any] = dist_group.WORLD, sync_grads: bool = False) -> Tensor:
        if group is not None and group != dist_group.WORLD:
            raise ValueError("Horovod does not support allgather using a subcommunicator at this time. Unset `group`.")

        if len(result.shape) == 0:
            # Convert scalars to single dimension tensors
            result = result.reshape(1)

        # sync and gather all
        self.join()
        return hvd.allgather(result)

    def post_backward(self, closure_loss: Tensor) -> None:
        # synchronize all horovod optimizers.
        for optimizer in self.optimizers:
            optimizer.synchronize()

    def _wrap_optimizers(
        self, optimizers: List[Optimizer], accumulate_grad_batches: int
    ) -> List["hvd.DistributedOptimizer"]:
        """Wrap optimizers to perform gradient aggregation via allreduce."""
        assert self.lightning_module is not None
        return [
            hvd.DistributedOptimizer(
                opt,
                backward_passes_per_step=accumulate_grad_batches,
                named_parameters=self._filter_named_parameters(self.lightning_module, opt),
            )
            if "horovod" not in str(opt.__class__)
            else opt
            for opt in optimizers
        ]

    @staticmethod
    def _filter_named_parameters(model: nn.Module, optimizer: Optimizer) -> List[Tuple[str, nn.Parameter]]:
        opt_params = {p for group in optimizer.param_groups for p in group.get("params", [])}
        return [(name, p) for name, p in model.named_parameters() if p in opt_params]

    @classmethod
    def register_strategies(cls, strategy_registry: Dict) -> None:
        """Register strategy."""
        strategy_registry.register(
            cls.strategy_name,
            cls,
            description=f"{cls.__class__.__name__}",
        )

    def teardown(self) -> None:
        """Teardown may be called before `_exit_stack` is set."""
        if self._exit_stack:
            self._exit_stack.__exit__(None, None, None)
            self._exit_stack = None
        # Make sure all workers have finished training before returning to the user
        self.join()
        super().teardown()
