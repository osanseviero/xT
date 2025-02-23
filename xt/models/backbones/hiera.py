from torch import nn

import hiera


class HieraWrapper(nn.Module):
    def __init__(self, model, hidden_size=768):
        super().__init__()
        self.model = model
        self.model.head = nn.Identity()
        self.model.norm = nn.Identity()
        self.feature_info = [dict(num_chs=hidden_size, reduction=32, module="cls_pool")]

    def forward(self, x):
        _, intermediates = self.model(x, return_intermediates=True)
        return [intermediates[-1].permute(0, 3, 1, 2)]  # n  768 7 7


def hiera_tiny_224(*args, **kwargs):
    model = hiera.hiera_tiny_224(pretrained=True, checkpoint="mae_in1k_ft_in1k")
    return HieraWrapper(model)


def hiera_small_224(*args, **kwargs):
    model = hiera.hiera_small_224(pretrained=True, checkpoint="mae_in1k_ft_in1k")
    return HieraWrapper(model)


def hiera_base_224(*args, **kwargs):
    model = hiera.hiera_base_224(pretrained=True, checkpoint="mae_in1k_ft_in1k")
    return HieraWrapper(model)


def hiera_base_plus_224(*args, **kwargs):
    model = hiera.hiera_base_plus_224(pretrained=True, checkpoint="mae_in1k_ft_in1k")
    return HieraWrapper(model, hidden_size=896)


def hiera_base_plus_448(*args, **kwargs):
    model = hiera.hiera_base_plus_224(
        pretrained=True, input_size=(448, 448), checkpoint="mae_in1k_ft_in1k"
    )
    return HieraWrapper(model, hidden_size=896)
