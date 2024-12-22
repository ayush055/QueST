"""
This file contains all neural modules related to encoding the spatial
information of obs_t, i.e., the abstracted knowledge of the current visual
input conditioned on the language.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
from torchvision.models._utils import IntermediateLayerGetter
from positional_encodings.torch_encodings import PositionalEncodingPermute2D, Summer


from quest.algos.baseline_modules.act_utils.misc import NestedTensor, is_main_process


###############################################################################
#
# Modules related to encoding visual information (can conditioned on language)
#
###############################################################################

class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt,
    without which any other policy_models than torchvision.policy_models.resnet[18,34,50,101]
    produce nans.

    From ACT codebase
    """

    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias


class PatchEncoder(nn.Module):
    """
    A patch encoder that does a linear projection of patches in a RGB image.
    """

    def __init__(
        self, input_shape, patch_size=[16, 16], embed_size=64, no_patch_embed_bias=False
    ):
        super().__init__()
        C, H, W = input_shape
        num_patches = (H // patch_size[0] // 2) * (W // patch_size[1] // 2)
        self.img_size = (H, W)
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.h, self.w = H // patch_size[0] // 2, W // patch_size[1] // 2

        self.conv = nn.Sequential(
            nn.Conv2d(
                C, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False
            ),
            nn.BatchNorm2d(
                64, eps=1e-05, momentum=0.1, affine=True, track_running_stats=True
            ),
            nn.ReLU(inplace=True),
        )
        self.proj = nn.Conv2d(
            64,
            embed_size,
            kernel_size=patch_size,
            stride=patch_size,
            bias=False if no_patch_embed_bias else True,
        )
        self.bn = nn.BatchNorm2d(embed_size)

    def forward(self, x):
        B, C, H, W = x.shape
        x = self.conv(x)
        x = self.proj(x)
        x = self.bn(x)
        return x


class SpatialSoftmax(nn.Module):
    """
    The spatial softmax layer (https://rll.berkeley.edu/dsae/dsae.pdf)
    """

    def __init__(self, in_c, in_h, in_w, num_kp=None):
        super().__init__()
        self._spatial_conv = nn.Conv2d(in_c, num_kp, kernel_size=1)

        pos_x, pos_y = torch.meshgrid(
            torch.linspace(-1, 1, in_w).float(),
            torch.linspace(-1, 1, in_h).float(),
            indexing="ij",
        )

        pos_x = pos_x.reshape(1, in_w * in_h)
        pos_y = pos_y.reshape(1, in_w * in_h)
        self.register_buffer("pos_x", pos_x)
        self.register_buffer("pos_y", pos_y)

        if num_kp is None:
            self._num_kp = in_c
        else:
            self._num_kp = num_kp

        self._in_c = in_c
        self._in_w = in_w
        self._in_h = in_h

    def forward(self, x):
        assert x.shape[1] == self._in_c
        assert x.shape[2] == self._in_h
        assert x.shape[3] == self._in_w

        h = x
        if self._num_kp != self._in_c:
            h = self._spatial_conv(h)
        h = h.contiguous().view(-1, self._in_h * self._in_w)

        attention = F.softmax(h, dim=-1)
        keypoint_x = (
            (self.pos_x * attention).sum(1, keepdims=True).view(-1, self._num_kp)
        )
        keypoint_y = (
            (self.pos_y * attention).sum(1, keepdims=True).view(-1, self._num_kp)
        )
        keypoints = torch.cat([keypoint_x, keypoint_y], dim=1)
        return keypoints


class SpatialProjection(nn.Module):
    def __init__(self, input_shape, out_dim):
        super().__init__()

        assert (
            len(input_shape) == 3
        ), "[error] spatial projection: input shape is not a 3-tuple"
        in_c, in_h, in_w = input_shape
        num_kp = out_dim // 2
        self.out_dim = out_dim
        self.spatial_softmax = SpatialSoftmax(in_c, in_h, in_w, num_kp=num_kp)
        self.projection = nn.Linear(num_kp * 2, out_dim)

    def forward(self, x):
        out = self.spatial_softmax(x)
        out = self.projection(out)
        return out

    def output_shape(self, input_shape):
        return input_shape[:-3] + (self.out_dim,)


class ResnetEncoder(nn.Module):
    """
    A Resnet-18-based encoder for mapping an image to a latent vector

    Encode (f) an image into a latent vector.

    y = f(x), where
        x: (B, C, H, W)
        y: (B, H_out)

    Args:
        input_shape:      (C, H, W), the shape of the image
        output_size:      H_out, the latent vector size
        pretrained:       whether use pretrained resnet
        freeze: whether   freeze the pretrained resnet
        remove_layer_num: remove the top # layers
        no_stride:        do not use striding
    """

    def __init__(
        self,
        input_shape,
        output_size,
        pretrained=False,
        freeze=False,
        remove_layer_num=2,
        no_stride=False,
        language_dim=768,
        language_fusion="film",
        do_projection=True,
        keep_tokens=False,
    ):

        super().__init__()

        ### 1. encode input (images) using convolutional layers
        assert remove_layer_num <= 5, "[error] please only remove <=5 layers"
        weights = torchvision.models.ResNet18_Weights.DEFAULT if pretrained else None
        layers = list(torchvision.models.resnet18(weights=weights).children())[
            :-remove_layer_num
        ]
        self.remove_layer_num = remove_layer_num

        assert (
            len(input_shape) == 3
        ), "[error] input shape of resnet should be (C, H, W)"

        in_channels = input_shape[0]
        if in_channels != 3:  # has eye_in_hand, increase channel size
            conv0 = nn.Conv2d(
                in_channels=in_channels,
                out_channels=64,
                kernel_size=(7, 7),
                stride=(2, 2),
                padding=(3, 3),
                bias=False,
            )
            layers[0] = conv0

        self.no_stride = no_stride
        if self.no_stride:
            layers[0].stride = (1, 1)
            layers[3].stride = 1

        self.keep_tokens = keep_tokens
        if not self.keep_tokens:
            self.resnet18_base = nn.Sequential(*layers[:4])
            self.block_1 = layers[4][0]
            self.block_2 = layers[4][1]
            self.block_3 = layers[5][0]
            self.block_4 = layers[5][1]
        else:            
            self.resnet18_base = nn.Sequential(*layers[:6])
            self.block_1 = layers[6][0]
            self.block_2 = layers[6][1]
            self.block_3 = layers[7][0]
            self.block_4 = layers[7][1]

        self.language_fusion = language_fusion
        if language_fusion != "none":
            self.lang_proj1 = nn.Linear(language_dim, 64 * 2)
            self.lang_proj2 = nn.Linear(language_dim, 64 * 2)
            self.lang_proj3 = nn.Linear(language_dim, 128 * 2)
            self.lang_proj4 = nn.Linear(language_dim, 128 * 2)

        if freeze:
            if in_channels != 3:
                raise Exception(
                    "[error] cannot freeze pretrained "
                    + "resnet with the extra eye_in_hand input"
                )
            for param in self.resnet18_embeddings.parameters():
                param.requires_grad = False

        if pretrained:
            self.normalizer = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])
        else:
            self.normalizer = nn.Identity()
                                    

        x = torch.zeros(1, *input_shape)
        y = self.block_4(
            self.block_3(self.block_2(self.block_1(self.resnet18_base(x))))
        )
        output_shape = y.shape  # compute the out dim

        do_projection = False if self.keep_tokens else do_projection  # do not need spatial softmax if we're using vision tokens

        if do_projection:
            ### 2. project the encoded input to a latent space
            self.projection_layer = SpatialProjection(output_shape[1:], output_size)
            self.out_channels = self.projection_layer(y).shape[1]
        elif self.keep_tokens:
            self.projection_layer = nn.Conv2d(output_shape[1], output_size, kernel_size=1, stride=1)
            self.positional_encoding = Summer(PositionalEncodingPermute2D(output_size))
            self.out_channels = output_size
        else:
            self.projection_layer = None
            self.out_channels = y.shape[-1]

    def forward(self, x, langs=None):
        x = self.normalizer(x)
        h = self.resnet18_base(x)

        h = self.block_1(h)
        if langs is not None and self.language_fusion != "none":  # FiLM layer
            B, C, H, W = h.shape
            beta, gamma = torch.split(
                self.lang_proj1(langs).reshape(B, C * 2, 1, 1), [C, C], 1
            )
            h = (1 + gamma) * h + beta

        h = self.block_2(h)
        if langs is not None and self.language_fusion != "none":  # FiLM layer
            B, C, H, W = h.shape
            beta, gamma = torch.split(
                self.lang_proj2(langs).reshape(B, C * 2, 1, 1), [C, C], 1
            )
            h = (1 + gamma) * h + beta

        h = self.block_3(h)
        if langs is not None and self.language_fusion != "none":  # FiLM layer
            B, C, H, W = h.shape
            beta, gamma = torch.split(
                self.lang_proj3(langs).reshape(B, C * 2, 1, 1), [C, C], 1
            )
            h = (1 + gamma) * h + beta

        h = self.block_4(h)
        if langs is not None and self.language_fusion != "none":  # FiLM layer
            B, C, H, W = h.shape
            beta, gamma = torch.split(
                self.lang_proj4(langs).reshape(B, C * 2, 1, 1), [C, C], 1
            )
            h = (1 + gamma) * h + beta

        if self.projection_layer is not None:
            h = self.projection_layer(h)

        if self.keep_tokens:
            B, C, H, W = h.shape
            h = self.positional_encoding(h)
            h = h.reshape(B, H * W, C)

        return h

    def update_resnet_stride(self, layer, stride):
        # Ensure the layer has the attributes
        if hasattr(layer, 'conv1') and hasattr(layer, 'conv2'):
            layer.conv1.stride = (stride, stride)
            layer.conv2.stride = (stride, stride)
            layer.downsample[0].stride = (stride, stride)
        return layer



class DINOEncoder(nn.Module):

    def __init__(
        self,
        input_shape,
        output_size,
        pretrained=True,
    ):
        super().__init__()
        assert (
            len(input_shape) == 3
        ), "[error] input shape of resnet should be (C, H, W)"

        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        self.preprocess = torchvision.transforms.Compose(
            [
                torchvision.transforms.Resize(224, interpolation=3),
                torchvision.transforms.Normalize(mean=mean, std=std),
            ]
        )
        self.dino = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14')
        self.mlp_block = nn.Sequential(
            nn.Linear(384, 64),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(64, 1),
            nn.Dropout(0.1),
        )
        self.projection = nn.Linear(384, output_size)
        self.output_shape = output_size
    
        if pretrained:
            for param in self.dino.parameters():
                param.requires_grad = False

    def forward(self, x, langs=None):
        x = self.preprocess(x)
        x = self.dino(x,is_training=True)['x_norm_patchtokens']
        mask = self.mlp_block(x).permute(0, 2, 1)
        mask = F.softmax(mask, dim=-1)
        x = torch.einsum('...si,...id->...sd', mask, x)
        x = self.projection(x)
        return x

    def output_shape(self, input_shape, shape_meta):
        return self.output_shape


