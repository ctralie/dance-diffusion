#!/usr/bin/env python3

from prefigure.prefigure import get_all_args
from copy import deepcopy
import math

import sys
import torch
from torch import optim
from torch.nn import functional as F
from torch.utils import data
from tqdm import trange
import pytorch_lightning as pl
from pytorch_lightning.utilities.rank_zero import rank_zero_only
from einops import rearrange

from dataset.dataset import SampleDataset

from audio_diffusion.models import DiffusionAttnUnet1D
from audio_diffusion.utils import ema_update, get_alphas_sigmas, get_crash_schedule
from viz.viz import audio_spectrogram_image



@torch.no_grad()
def sample(model, x, steps, eta):
    """Draws samples from a model given starting noise."""
    ts = x.new_ones([x.shape[0]])

    # Create the noise schedule
    t = torch.linspace(1, 0, steps + 1)[:-1]

    t = get_crash_schedule(t)

    alphas, sigmas = get_alphas_sigmas(t)

    # The sampling loop
    for i in trange(steps):

        # Get the model output (v, the predicted velocity)
        with torch.cuda.amp.autocast():
            v = model(x, ts * t[i]).float()

        # Predict the noise and the denoised image
        pred = x * alphas[i] - v * sigmas[i]
        eps = x * sigmas[i] + v * alphas[i]

        # If we are not on the last timestep, compute the noisy image for the
        # next timestep.
        if i < steps - 1:
            # If eta > 0, adjust the scaling factor for the predicted noise
            # downward according to the amount of additional noise to add
            ddim_sigma = eta * (sigmas[i + 1]**2 / sigmas[i]**2).sqrt() * \
                (1 - alphas[i]**2 / alphas[i + 1]**2).sqrt()
            adjusted_sigma = (sigmas[i + 1]**2 - ddim_sigma**2).sqrt()

            # Recombine the predicted noise and predicted denoised image in the
            # correct proportions for the next step
            x = pred * alphas[i + 1] + eps * adjusted_sigma

            # Add the correct amount of fresh noise
            if eta:
                x += torch.randn_like(x) * ddim_sigma

    # If we are on the last timestep, output the denoised image
    return pred



class DiffusionUncond(pl.LightningModule):
    def __init__(self, global_args):
        super().__init__()

        self.diffusion = DiffusionAttnUnet1D(global_args, io_channels=1, n_attn_layers=4)
        self.diffusion_ema = deepcopy(self.diffusion)
        self.rng = torch.quasirandom.SobolEngine(1, scramble=True, seed=global_args.seed)
        self.ema_decay = global_args.ema_decay
        
    def configure_optimizers(self):
        return optim.Adam([*self.diffusion.parameters()], lr=4e-5)
  
    def training_step(self, batch, batch_idx):
        reals = batch[0]

        # Draw uniformly distributed continuous timesteps
        t = self.rng.draw(reals.shape[0])[:, 0].to(self.device)

        t = get_crash_schedule(t)

        # Calculate the noise schedule parameters for those timesteps
        alphas, sigmas = get_alphas_sigmas(t)

        # Combine the ground truth images and the noise
        alphas = alphas[:, None, None]
        sigmas = sigmas[:, None, None]
        noise = torch.randn_like(reals)
        noised_reals = reals * alphas + noise * sigmas
        targets = noise * alphas - reals * sigmas

        with torch.cuda.amp.autocast():
            v = self.diffusion(noised_reals, t)
            mse_loss = F.mse_loss(v, targets)
            loss = mse_loss

        self.log("train_loss", loss.detach())
        self.log("train_mse_loss", mse_loss.detach())
        return loss
    
    def validation_step(self, batch, batch_idx):
        reals = batch[0]

        # Draw uniformly distributed continuous timesteps
        t = self.rng.draw(reals.shape[0])[:, 0].to(self.device)

        t = get_crash_schedule(t)

        # Calculate the noise schedule parameters for those timesteps
        alphas, sigmas = get_alphas_sigmas(t)

        # Combine the ground truth images and the noise
        alphas = alphas[:, None, None]
        sigmas = sigmas[:, None, None]
        noise = torch.randn_like(reals)
        noised_reals = reals * alphas + noise * sigmas
        targets = noise * alphas - reals * sigmas

        with torch.cuda.amp.autocast():
            with torch.no_grad():
                v = self.diffusion(noised_reals, t)
                mse_loss = F.mse_loss(v, targets)
                loss = mse_loss

        self.log("valid_loss", loss.detach())
        self.log("valid_mse_loss", mse_loss.detach())

    def on_before_zero_grad(self, *args, **kwargs):
        decay = 0.95 if self.current_epoch < 25 else self.ema_decay
        ema_update(self.diffusion, self.diffusion_ema, decay)

class ExceptionCallback(pl.Callback):
    def on_exception(self, trainer, module, err):
        print(f'{type(err).__name__}: {err}', file=sys.stderr)


class DemoCallback(pl.Callback):
    def __init__(self, global_args):
        super().__init__()
        self.num_demos = global_args.num_demos
        self.demo_samples = global_args.sample_size
        self.demo_every_n_epochs = global_args.demo_every_n_epochs
        self.demo_steps = global_args.demo_steps
        self.sample_rate = global_args.sample_rate
        self.epoch_num = 0

    @rank_zero_only
    @torch.no_grad()
    def on_train_epoch_end(self, trainer, module):
        self.epoch_num += 1
        if self.epoch_num % self.demo_every_n_epochs == 0:
            noise = torch.randn([self.num_demos, 1, self.demo_samples]).to(module.device)
            try:
                fakes = sample(module.diffusion_ema, noise, self.demo_steps, 0)

                # Put the demos together
                fakes = rearrange(fakes, 'b d n -> d (b n)')
                trainer.logger.experiment.add_audio("audio_val", fakes.cpu(), trainer.global_step,
                                                self.sample_rate)
                trainer.logger.experiment.add_image("audio_specgram", audio_spectrogram_image(fakes.detach().cpu(), sample_rate=self.sample_rate), dataformats="HWC")
                
            except Exception as e:
                print(f'{type(e).__name__}: {e}', file=sys.stderr)

def main():

    args = get_all_args()

    args.latent_dim = 0

    save_path = None if args.save_path == "" else args.save_path

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print('Using device:', device)
    torch.manual_seed(args.seed)

    train_set = SampleDataset([args.training_dir], args, train=True)
    train_dl = data.DataLoader(train_set, args.batch_size, shuffle=True,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True, drop_last=True)
    valid_dl = None
    if len(args.validation_dir) > 0:
        valid_set = SampleDataset([args.validation_dir], args, train=False)
        valid_dl = data.DataLoader(valid_set, args.batch_size, False, num_workers=args.num_workers)
    tensorboard_logger=pl.loggers.TensorBoardLogger(
        args.log_path,
        name=args.name,
    ),

    exc_callback = ExceptionCallback()
    ckpt_callback = pl.callbacks.ModelCheckpoint(every_n_train_steps=args.checkpoint_every, save_top_k=-1, dirpath=save_path)
    demo_callback = DemoCallback(args)

    diffusion_model = DiffusionUncond(args)

    diffusion_trainer = pl.Trainer(
        devices=args.num_gpus,
        accelerator="gpu",
        # num_nodes = args.num_nodes,
        # strategy='ddp',
        precision=16,
        accumulate_grad_batches=args.accum_batches, 
        callbacks=[ckpt_callback, demo_callback, exc_callback],
        logger=tensorboard_logger,
        log_every_n_steps=1,
        max_epochs=10000000,
    )

    diffusion_trainer.fit(diffusion_model, train_dl, valid_dl, ckpt_path=args.ckpt_path)

if __name__ == '__main__':
    main()

