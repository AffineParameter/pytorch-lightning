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

import logging
from contextlib import suppress
from typing import List, Optional, Tuple

from torch.optim import Optimizer

import pytorch_lightning as pl
from pytorch_lightning.loops.base import Loop
from pytorch_lightning.loops.training_epoch_loop import TrainingEpochLoop
from pytorch_lightning.trainer.connectors.logger_connector.result import ResultCollection
from pytorch_lightning.trainer.supporters import TensorRunningAccum
from pytorch_lightning.utilities import rank_zero_info

log = logging.getLogger(__name__)


class FitLoop(Loop):

    def __init__(
        self,
        min_epochs: Optional[int] = None,
        max_epochs: Optional[int] = None,
        min_steps: Optional[int] = None,
        max_steps: Optional[int] = None
    ):
        super().__init__()
        self._teardown_already_run = False

        # If neither max_epochs or max_steps is set, then use existing default of max_epochs = 1000
        self.max_epochs = 1000 if (max_epochs is None and max_steps is None) else max_epochs
        # If neither min_epochs or min_steps is set, then use existing default of min_epochs = 1
        self.min_epochs = 1 if (min_epochs is None and min_steps is None) else min_epochs

        self.training_loop = TrainingEpochLoop(min_steps, max_steps)
        self.results = ResultCollection(True)

    @property
    def current_epoch(self) -> int:
        return self.iteration_count

    @current_epoch.setter
    def current_epoch(self, value: int):
        self.iteration_count = value

    @property
    def global_step(self):
        return self.training_loop.global_step

    @global_step.setter
    def global_step(self, value):
        self.training_loop.global_step = value

    @property
    def total_batch_idx(self):
        return self.training_loop.total_batch_idx

    @property
    def batch_idx(self):
        return self.training_loop.iteration_count

    @property
    def split_idx(self):
        return self.training_loop.split_idx

    @property
    def min_steps(self):
        return self.training_loop.min_steps

    @property
    def max_steps(self):
        return self.training_loop.max_steps

    @max_steps.setter
    def max_steps(self, value):
        # TODO(@awaelchli): This setter is required by debugging connector (fast dev run), should be avoided
        self.training_loop.max_steps = value

    @property
    def running_loss(self):
        return self.training_loop.batch_loop.running_loss

    @property
    def skip_backward(self) -> bool:
        """ Determines whether the loop will skip backward during automatic optimization. """
        return self.training_loop.batch_loop.skip_backward

    @skip_backward.setter
    def skip_backward(self, value: bool):
        """ Determines whether the loop will skip backward during automatic optimization. """
        self.training_loop.batch_loop.skip_backward = value

    @property
    def done(self) -> bool:
        # TODO(@awaelchli): Move track steps inside training loop and move part of these condition inside training loop
        stop_steps = self.max_steps is not None and self.global_step >= self.max_steps
        stop_epochs = self.max_epochs is not None and self.current_epoch >= self.max_epochs

        should_stop = False
        if self.trainer.should_stop:
            # early stopping
            met_min_epochs = self.current_epoch >= self.min_epochs if self.min_epochs else True
            met_min_steps = self.global_step >= self.min_steps if self.min_steps else True
            if met_min_epochs and met_min_steps:
                should_stop = True
            else:
                log.info(
                    'Trainer was signaled to stop but required minimum epochs'
                    f' ({self.min_epochs}) or minimum steps ({self.min_steps}) has'
                    ' not been met. Training will continue...'
                )
                self.trainer.should_stop = False

        return stop_steps or should_stop or stop_epochs

    def connect(self, trainer: 'pl.Trainer', *args, **kwargs):
        self.trainer = trainer
        self.training_loop.connect(trainer)

    def reset(self) -> None:
        self.iteration_count = 0

    def run(self):
        if not self._should_skip_training():
            return super().run()

    def on_run_start(self):
        self.trainer.results.to(device=self.trainer.lightning_module.device)
        self.trainer.call_hook("on_train_start")

    def on_advance_start(self):
        model = self.trainer.lightning_module

        # reset train dataloader
        if self.current_epoch != 0 and self.trainer.reload_dataloaders_every_epoch:
            self.trainer.reset_train_dataloader(model)

        # TODO: specify the possible exception
        with suppress(Exception):
            # set seed for distributed sampler (enables shuffling for each epoch)
            self.trainer.train_dataloader.sampler.set_epoch(self.current_epoch)

        # changing gradient according accumulation_scheduler
        self.trainer.accumulation_scheduler.on_train_epoch_start(self.trainer, self.trainer.lightning_module)

        # stores accumulated grad fractions per batch
        self.training_loop.batch_loop.accumulated_loss = TensorRunningAccum(
            window_length=self.trainer.accumulate_grad_batches
        )

        # hook
        self.trainer.logger_connector.on_epoch_start()
        self.trainer.call_hook("on_epoch_start")
        self.trainer.call_hook("on_train_epoch_start")

    def advance(self):
        train_dataloader = self.trainer.accelerator.process_dataloader(self.trainer.train_dataloader)
        train_dataloader = self.trainer.data_connector.get_profiled_train_dataloader(train_dataloader)

        with self.trainer.profiler.profile("run_training_epoch"):
            # run train epoch
            epoch_output = self.training_loop.run(train_dataloader)
            # log epoch metrics

            if epoch_output is None:
                return

            # the global step is manually decreased here due to backwards compatibility with existing loggers
            # as they expect that the same step is used when logging epoch end metrics even when the batch loop has
            # finished. this means the attribute does not exactly track the number of optimizer steps applied.
            # TODO(@carmocca): deprecate and rename so users don't get confused
            self.global_step -= 1
            # log epoch metrics
            self.trainer.logger_connector.update_train_epoch_metrics()
            self.global_step += 1

    def on_advance_end(self):
        if self.training_loop.batches_seen == 0:
            return

        self.training_loop.update_lr_schedulers('epoch')

        did_train_only = self.trainer.disable_validation or self.trainer.evaluation_loop.should_skip_evaluation(
            self.trainer.num_val_batches
        )
        if did_train_only:
            self.global_step -= 1
            self.check_checkpoint_callback(True)
            self.global_step += 1

    def on_run_end(self):
        if self._teardown_already_run:
            return
        self._teardown_already_run = True

        # NOTE: the iteration_count/current_epoch is already incremented
        # Lightning today does not increment the current epoch at the last epoch run in Trainer.fit
        # To simulate that current behavior, we decrement here.
        self.current_epoch -= 1

        # trigger checkpoint check. need to temporarily decrease the global step to avoid saving duplicates
        # when a checkpoint was saved at the last step
        self.training_loop.global_step -= 1
        # TODO: see discussion/rework https://github.com/PyTorchLightning/pytorch-lightning/issues/7406
        self.check_checkpoint_callback(should_update=True, is_last=True)
        self.training_loop.global_step += 1

        # hook
        self.trainer.call_hook("on_train_end")

        # todo: TPU 8 cores hangs in flush with TensorBoard. Might do for all loggers.
        # It might be related to xla tensors blocked when moving the cpu
        # kill loggers
        if self.trainer.logger is not None:
            self.trainer.logger.finalize("success")

        # summarize profile results
        self.trainer.profiler.describe()

        # give accelerators a chance to finish
        self.trainer.accelerator.on_train_end()

        # reset bookkeeping
        self.trainer._running_stage = None

    def _should_skip_training(self) -> bool:
        return self.done or self.trainer.num_training_batches == 0

    def should_accumulate(self):
        return self.training_loop.batch_loop.should_accumulate()

    def get_active_optimizers(self, batch_idx: Optional[int] = None) -> List[Tuple[int, Optimizer]]:
        return self.training_loop.batch_loop.get_active_optimizers(batch_idx)

    def check_checkpoint_callback(self, should_update, is_last=False):
        # TODO: bake this logic into the ModelCheckpoint callback
        if should_update and self.trainer.checkpoint_connector.has_trained:
            callbacks = self.trainer.checkpoint_callbacks

            if is_last and any(cb.save_last and cb.verbose for cb in callbacks):
                rank_zero_info("Saving latest checkpoint...")

            model = self.trainer.lightning_module

            for cb in callbacks:
                cb.on_validation_end(self.trainer, model)
