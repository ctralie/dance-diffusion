from contextlib import contextmanager
import warnings

import torch
from torch import nn 
import random 
import math
from torch import optim

############################################################
##                Utilities for training
############################################################

@contextmanager
def train_mode(model, mode=True):
    """A context manager that places a model into training mode and restores
    the previous mode on exit."""
    modes = [module.training for module in model.modules()]
    try:
        yield model.train(mode)
    finally:
        for i, module in enumerate(model.modules()):
            module.training = modes[i]


def eval_mode(model):
    """A context manager that places a model into evaluation mode and restores
    the previous mode on exit."""
    return train_mode(model, False)

@torch.no_grad()
def ema_update(model, averaged_model, decay):
    """Incorporates updated model parameters into an exponential moving averaged
    version of a model. It should be called after each optimizer step."""
    model_params = dict(model.named_parameters())
    averaged_params = dict(averaged_model.named_parameters())
    assert model_params.keys() == averaged_params.keys()

    for name, param in model_params.items():
        averaged_params[name].mul_(decay).add_(param, alpha=1 - decay)

    model_buffers = dict(model.named_buffers())
    averaged_buffers = dict(averaged_model.named_buffers())
    assert model_buffers.keys() == averaged_buffers.keys()

    for name, buf in model_buffers.items():
        averaged_buffers[name].copy_(buf)


class EMAWarmup:
    """Implements an EMA warmup using an inverse decay schedule.
    If inv_gamma=1 and power=1, implements a simple average. inv_gamma=1, power=2/3 are
    good values for models you plan to train for a million or more steps (reaches decay
    factor 0.999 at 31.6K steps, 0.9999 at 1M steps), inv_gamma=1, power=3/4 for models
    you plan to train for less (reaches decay factor 0.999 at 10K steps, 0.9999 at
    215.4k steps).
    Args:
        inv_gamma (float): Inverse multiplicative factor of EMA warmup. Default: 1.
        power (float): Exponential factor of EMA warmup. Default: 1.
        min_value (float): The minimum EMA decay rate. Default: 0.
        max_value (float): The maximum EMA decay rate. Default: 1.
        start_at (int): The epoch to start averaging at. Default: 0.
        last_epoch (int): The index of last epoch. Default: 0.
    """

    def __init__(self, inv_gamma=1., power=1., min_value=0., max_value=1., start_at=0,
                 last_epoch=0):
        self.inv_gamma = inv_gamma
        self.power = power
        self.min_value = min_value
        self.max_value = max_value
        self.start_at = start_at
        self.last_epoch = last_epoch

    def state_dict(self):
        """Returns the state of the class as a :class:`dict`."""
        return dict(self.__dict__.items())

    def load_state_dict(self, state_dict):
        """Loads the class's state.
        Args:
            state_dict (dict): scaler state. Should be an object returned
                from a call to :meth:`state_dict`.
        """
        self.__dict__.update(state_dict)

    def get_value(self):
        """Gets the current EMA decay rate."""
        epoch = max(0, self.last_epoch - self.start_at)
        value = 1 - (1 + epoch / self.inv_gamma) ** -self.power
        return 0. if epoch < 0 else min(self.max_value, max(self.min_value, value))

    def step(self):
        """Updates the step count."""
        self.last_epoch += 1


class InverseLR(optim.lr_scheduler._LRScheduler):
    """Implements an inverse decay learning rate schedule with an optional exponential
    warmup. When last_epoch=-1, sets initial lr as lr.
    inv_gamma is the number of steps/epochs required for the learning rate to decay to
    (1 / 2)**power of its original value.
    Args:
        optimizer (Optimizer): Wrapped optimizer.
        inv_gamma (float): Inverse multiplicative factor of learning rate decay. Default: 1.
        power (float): Exponential factor of learning rate decay. Default: 1.
        warmup (float): Exponential warmup factor (0 <= warmup < 1, 0 to disable)
            Default: 0.
        final_lr (float): The final learning rate. Default: 0.
        last_epoch (int): The index of last epoch. Default: -1.
        verbose (bool): If ``True``, prints a message to stdout for
            each update. Default: ``False``.
    """

    def __init__(self, optimizer, inv_gamma=1., power=1., warmup=0., final_lr=0.,
                 last_epoch=-1, verbose=False):
        self.inv_gamma = inv_gamma
        self.power = power
        if not 0. <= warmup < 1:
            raise ValueError('Invalid value for warmup')
        self.warmup = warmup
        self.final_lr = final_lr
        super().__init__(optimizer, last_epoch, verbose)

    def get_lr(self):
        if not self._get_lr_called_within_step:
            warnings.warn("To get the last learning rate computed by the scheduler, "
                          "please use `get_last_lr()`.")

        return self._get_closed_form_lr()

    def _get_closed_form_lr(self):
        warmup = 1 - self.warmup ** (self.last_epoch + 1)
        lr_mult = (1 + self.last_epoch / self.inv_gamma) ** -self.power
        return [warmup * max(self.final_lr, base_lr * lr_mult)
                for base_lr in self.base_lrs]



############################################################
##       Utilities for the diffusion noise schedule
############################################################
def get_alphas_sigmas(t):
    """Returns the scaling factors for the clean image (alpha) and for the
    noise (sigma), given a timestep."""
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)

def get_crash_schedule(t):
    sigma = torch.sin(t * math.pi / 2) ** 2
    alpha = (1 - sigma ** 2) ** 0.5
    return alpha_sigma_to_t(alpha, sigma)

def t_to_alpha_sigma(t):
    """Returns the scaling factors for the clean image and for the noise, given
    a timestep."""
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)

def alpha_sigma_to_t(alpha, sigma):
    """Returns a timestep, given the scaling factors for the clean image and for
    the noise."""
    return torch.atan2(sigma, alpha) / math.pi * 2


############################################################
##              Data Augmentation Tools
############################################################

class PadCrop(nn.Module):
    def __init__(self, n_samples, randomize=True):
        super().__init__()
        self.n_samples = n_samples
        self.randomize = randomize

    def __call__(self, signal):
        n, s = signal.shape
        start = 0 if (not self.randomize) else torch.randint(0, max(0, s - self.n_samples) + 1, []).item()
        end = start + self.n_samples
        output = signal.new_zeros([n, self.n_samples])
        output[:, :min(s, self.n_samples)] = signal[:, start:end]
        return output

class RandomPhaseInvert(nn.Module):
    def __init__(self, p=0.5):
        super().__init__()
        self.p = p
    def __call__(self, signal):
        return -signal if (random.random() < self.p) else signal

class RandomMix(nn.Module):
  def __call__(self, signal):
    """
    If multichannel, take a random convex combination of the channels
    """
    signal_shape = signal.shape
    if len(signal_shape) == 1: # s -> 1, s
        signal = signal.unsqueeze(0)
    elif len(signal_shape) == 2:
        n_channel = signal_shape[0]
        if n_channel > 1: #?, s -> 1, s
            weights = torch.rand(n_channel, 1).to(signal)
            weights /= torch.sum(weights)
            signal = torch.sum(weights*signal, dim=0, keepdims=True)    
    return signal
  
class EqualMix(nn.Module):
  def __call__(self, signal):
    """
    If multichannel, take a an equal combination of the channels
    """
    signal_shape = signal.shape
    if len(signal_shape) == 1: # s -> 1, s
        signal = signal.unsqueeze(0)
    elif len(signal_shape) == 2:
        n_channel = signal_shape[0]
        if n_channel > 1: #?, s -> 1, s
            weights = torch.ones(n_channel, 1).to(signal)
            weights /= n_channel
            signal = torch.sum(weights*signal, dim=0, keepdims=True)    
    return signal

class TempoStretch(nn.Module):
    def __init__(self, sr, tmin=0.8, tmax=1.2, frac=0.5):
        """
        Parameters
        ----------
        sr: int
            Audio sample rate
        tmin: float
            Minimum tempo warp ratio
        tmax: float
            Maximum tempo warp ratio
        frac: float
            Proportion of the time to warp the tempo
        """
        super().__init__()
        self.sr = sr
        self.tmin = tmin
        self.tmax = tmax
        self.frac = frac

    def __call__(self, signal):
        import numpy as np
        ret = signal
        if np.random.rand() < self.frac:
            import pyrubberband as pyrb
            fac = self.tmin + (self.tmax-self.tmin)*np.random.rand()
            ret = pyrb.time_stretch(signal.detach().cpu().numpy().T, self.sr, fac)
            ret = torch.from_numpy(ret.T).to(signal)
        return ret
    



############################################################
##       Utilities for inference and style transfer
############################################################

class DiffusionUncondInfer(nn.Module):
    def __init__(self, global_args):
        super().__init__()
        from .models import DiffusionAttnUnet1D
        from copy import deepcopy
        self.diffusion = DiffusionAttnUnet1D(global_args, io_channels=1, n_attn_layers=4)
        self.diffusion_ema = deepcopy(self.diffusion)
        self.rng = torch.quasirandom.SobolEngine(1, scramble=True, seed=global_args.seed)

def load_model_for_synthesis(ckpt_path, sample_size, sample_rate, device, latent_dim=0, seed=42):
    """
    Load a pretrained model for synthesis and style transfer
    """
    class Object(object):
        pass
    args = Object()
    args.sample_size = sample_size
    args.sample_rate = sample_rate
    args.latent_dim = latent_dim
    args.seed = seed
    model = DiffusionUncondInfer(args)
    model.load_state_dict(torch.load(ckpt_path)["state_dict"])
    model = model.requires_grad_(False).to(device)

    del model.diffusion
    return model.diffusion_ema


def do_style_transfer(model, audio_sample, steps, noise_level, device, eta=0):
    """
    DiffusionUncondInfer: model
        Model to use
    audio_sample: torch.tensor(batch, channel, samples)
        Original audio samples to send through the style transfer
    steps: int
        Maximum number of steps to take.  Can be fewer if noise_level < 1
    noise_level: float
        Initial noise level to apply.  The closer this is to 0, the more 
        the audio will sound like the original.  The closer it is to 1, the
        more the audio will sound like the model
    device: str
        Device to use
    eta: float
        Extra noise to use in diffusion    
    """
    from diffusion import sampling
    t = torch.linspace(0, 1, steps + 1, device=device)
    step_list = get_crash_schedule(t)
    step_list = step_list[step_list < noise_level]

    alpha, sigma = t_to_alpha_sigma(step_list[-1])

    noised = torch.randn(audio_sample.shape, device=device)
    noised = audio_sample * alpha + noised * sigma
    noise = noised

    return sampling.sample(model, noise, step_list.flip(0)[:-1], eta, {})


############################################################
##              Other Utilities
############################################################

def expand_to_planes(input, shape):
    return input[..., None].repeat([1, 1, shape[2]])

def load_to_device(path, sr, device):
    """
    Load audio to a device at the appropriate sample rate

    Parameters
    ----------
    path: str
        Path to audio
    sr: int
        Audio sample rate
    device: str
        Device to which to load the audio
    
    Returns
    -------
    Loaded audio
    """
    import torchaudio
    audio, file_sr = torchaudio.load(path)
    if sr != file_sr:
      audio = torchaudio.transforms.Resample(file_sr, sr)(audio)
    audio = audio.to(device)
    return audio

def append_dims(x, target_dims):
    """Appends dimensions to the end of a tensor until it has target_dims dimensions."""
    dims_to_append = target_dims - x.ndim
    if dims_to_append < 0:
        raise ValueError(f'input has {x.ndim} dims but target_dims is {target_dims}, which is less')
    return x[(...,) + (None,) * dims_to_append]


def n_params(module):
    """Returns the number of trainable parameters in a module."""
    return sum(p.numel() for p in module.parameters())