import torch
import torch.nn.functional as F
import numpy as np
import quest.utils.tensor_utils as TensorUtils
import itertools
from functools import partial

from quest.algos.base import ChunkPolicy
from quest.algos.utils.rgb_modules import DINOEncoder


class QueST(ChunkPolicy):
    def __init__(self,
                 autoencoder,
                 policy_prior,
                 stage,
                 loss_fn,
                 l1_loss_scale,
                 **kwargs
                 ):
        super().__init__(**kwargs)
        self.autoencoder = autoencoder
        self.policy_prior = policy_prior
        self.stage = stage

        self.start_token = self.policy_prior.start_token
        self.l1_loss_scale = l1_loss_scale if stage == 2 else 0
        self.codebook_size = np.array(autoencoder.fsq_level).prod()

        self.direct_skill_tokens = self.policy_prior.direct_skill_tokens
        
        self.loss = loss_fn
        
    def get_optimizers(self):
        optimizers = []
        name_blacklist = []

        # need to separate dino encoder params and create customer optimizer factory with different lr
        img_encoders_names, img_encoders_decay, img_encoders_no_decay = [], [], []
        for img_encoder_name, img_encoder in self.image_encoders.items():
            if isinstance(img_encoder, DINOEncoder) and not img_encoder.freeze:
                img_encoder_decay, img_encoder_no_decay = TensorUtils.separate_no_decay(img_encoder)
                img_encoders_decay.extend(img_encoder_decay)
                img_encoders_no_decay.extend(img_encoder_no_decay)
                img_encoders_names.append(img_encoder_name)
            else:
                continue

        if img_encoders_decay or img_encoders_no_decay:
            optimizers.extend([self.optimizer_factory(params=img_encoders_decay, lr=5e-6),
                            self.optimizer_factory(params=img_encoders_no_decay, weight_decay=0., lr=5e-6)])
            
            name_blacklist.extend(img_encoders_names)

        if self.stage == 0:
            decay, no_decay = TensorUtils.separate_no_decay(self.autoencoder)
            optimizers.extend([
                self.optimizer_factory(params=decay),
                self.optimizer_factory(params=no_decay, weight_decay=0.)
            ])
            return optimizers
        elif self.stage == 1:
            name_blacklist.append('autoencoder')
            print("rest of params")
            decay, no_decay = TensorUtils.separate_no_decay(self, 
                                                            name_blacklist=name_blacklist)

            optimizers.extend([
                self.optimizer_factory(params=decay),
                self.optimizer_factory(params=no_decay, weight_decay=0.)
            ])

            return optimizers
        elif self.stage == 2:
            name_blacklist.append('autoencoder')
            decay, no_decay = TensorUtils.separate_no_decay(self, 
                                                            name_blacklist=name_blacklist)
            decoder_decay, decoder_no_decay = TensorUtils.separate_no_decay(self.autoencoder.decoder)
            optimizers.extend([
                self.optimizer_factory(params=itertools.chain(decay, decoder_decay)),
                self.optimizer_factory(params=itertools.chain(no_decay, decoder_no_decay), weight_decay=0.)
            ])
            return optimizers

    def get_context(self, data):
        obs_emb = self.obs_encode(data)
        task_emb = self.get_task_emb(data).unsqueeze(1)
        context = torch.cat([task_emb, obs_emb], dim=1)
        return context

    def compute_loss(self, data):
        if self.stage == 0:
            return self.compute_autoencoder_loss(data)
        elif self.stage == 1:
            return self.compute_prior_loss(data)
        elif self.stage == 2:
            return self.compute_prior_loss(data)

    def compute_autoencoder_loss(self, data):
        pred, pp, pp_sample, aux_loss, _ = self.autoencoder(data["actions"])
        recon_loss = self.loss(pred, data["actions"])
        if self.autoencoder.vq_type == 'vq':
            loss = recon_loss + aux_loss
        else:
            loss = recon_loss
            
        info = {
            'loss': loss.item(),
            'recon_loss': recon_loss.item(),
            'aux_loss': aux_loss.sum().item(),
            'pp': pp.item(),
            'pp_sample': pp_sample.item(),
        }
        return loss, info

    def compute_prior_loss(self, data):
        data = self.preprocess_input(data, train_mode=True)
        with torch.no_grad():
            codes, indices = self.autoencoder.get_indices(data["actions"])
            indices = indices.long()
        context = self.get_context(data)
        if self.direct_skill_tokens:
            start_tokens = (torch.ones((context.shape[0], 1, codes.shape[-1]), device=self.device, dtype=torch.long) * self.start_token)
            x = torch.cat([start_tokens, codes[:,:-1,:]], dim=1)
        else:    
            start_tokens = (torch.ones((context.shape[0], 1), device=self.device, dtype=torch.long) * self.start_token)
            x = torch.cat([start_tokens, indices[:,:-1]], dim=1)
        targets = indices.clone()
        logits = self.policy_prior(x, context)
        prior_loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        
        with torch.no_grad():
            logits = logits[:,:,:self.codebook_size]
            probs = torch.softmax(logits, dim=-1)
            sampled_indices = torch.multinomial(probs.view(-1,logits.shape[-1]),1)
            sampled_indices = sampled_indices.view(-1,logits.shape[1])
        
        pred_actions = self.autoencoder.decode_actions(sampled_indices)
        l1_loss = self.loss(pred_actions, data["actions"])
        total_loss = prior_loss + self.l1_loss_scale * l1_loss
        info = {
            'loss': total_loss.item(),
            'nll_loss': prior_loss.item(),
            'l1_loss': l1_loss.item()
        }
        return total_loss, info

    def sample_actions(self, data):
        data = self.preprocess_input(data, train_mode=False)
        context = self.get_context(data)
        sampled_indices = self.policy_prior.get_indices_top_k(context, self.codebook_size)
        pred_actions = self.autoencoder.decode_actions(sampled_indices)
        pred_actions = pred_actions.permute(1,0,2)
        return pred_actions.detach().cpu().numpy()
