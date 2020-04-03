# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
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

"""BERT model."""

import torch

from megatron import get_args
from megatron.module import MegatronModule

from .language_model import parallel_lm_logits
from .language_model import get_language_model
from .transformer import LayerNorm
from .utils import gelu
from .utils import get_linear_layer
from .utils import init_method_normal
from .utils import scaled_init_method_normal


def bert_attention_mask_func(attention_scores, attention_mask):
    attention_scores = attention_scores + attention_mask
    return attention_scores


def bert_extended_attention_mask(attention_mask, dtype):
    # We create a 3D attention mask from a 2D tensor mask.
    # [b, 1, s]
    attention_mask_b1s = attention_mask.unsqueeze(1)
    # [b, s, 1]
    attention_mask_bs1 = attention_mask.unsqueeze(2)
    # [b, s, s]
    attention_mask_bss = attention_mask_b1s * attention_mask_bs1
    # [b, 1, s, s]
    extended_attention_mask = attention_mask_bss.unsqueeze(1)
    # Since attention_mask is 1.0 for positions we want to attend and 0.0
    # for masked positions, this operation will create a tensor which is
    # 0.0 for positions we want to attend and -10000.0 for masked positions.
    # Since we are adding it to the raw scores before the softmax, this is
    # effectively the same as removing these entirely.
    # fp16 compatibility
    extended_attention_mask = extended_attention_mask.to(dtype=dtype)
    extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

    return extended_attention_mask


def bert_position_ids(token_ids):
    # Create position ids
    seq_length = token_ids.size(1)
    position_ids = torch.arange(seq_length, dtype=torch.long,
                                device=token_ids.device)
    position_ids = position_ids.unsqueeze(0).expand_as(token_ids)

    return position_ids



class BertLMHead(MegatronModule):
    """Masked LM head for Bert

    Arguments:
        mpu_vocab_size: model parallel size of vocabulary.
        hidden_size: hidden size
        init_method: init method for weight initialization
        layernorm_epsilon: tolerance for layer norm divisions
        parallel_output: whether output logits being distributed or not.
    """
    def __init__(self, mpu_vocab_size, hidden_size, init_method,
                 layernorm_epsilon, parallel_output):

        super(BertLMHead, self).__init__()

        self.bias = torch.nn.Parameter(torch.zeros(mpu_vocab_size))
        self.bias.model_parallel = True
        self.bias.partition_dim = 0
        self.bias.stride = 1
        self.parallel_output = parallel_output

        self.dense = get_linear_layer(hidden_size, hidden_size, init_method)
        self.layernorm = LayerNorm(hidden_size, eps=layernorm_epsilon)


    def forward(self, hidden_states, word_embeddings_weight):
        hidden_states = self.dense(hidden_states)
        hidden_states = gelu(hidden_states)
        hidden_states = self.layernorm(hidden_states)
        output = parallel_lm_logits(hidden_states,
                                    word_embeddings_weight,
                                    self.parallel_output,
                                    bias=self.bias)
        return output



class BertModel(MegatronModule):
    """Bert Language model."""

    def __init__(self, num_tokentypes=2, add_binary_head=True,
                 ict_head_size=None, parallel_output=True):
        super(BertModel, self).__init__()
        args = get_args()

        self.add_binary_head = add_binary_head
        self.ict_head_size = ict_head_size
        self.add_ict_head = ict_head_size is not None
        assert not (self.add_binary_head and self.add_ict_head)

        self.parallel_output = parallel_output
        init_method = init_method_normal(args.init_method_std)
        add_pooler = self.add_binary_head or self.add_ict_head
        scaled_init_method = scaled_init_method_normal(args.init_method_std,
                                                       args.num_layers)
        self.language_model, self._language_model_key = get_language_model(
            attention_mask_func=bert_attention_mask_func,
            num_tokentypes=num_tokentypes,
            add_pooler=add_pooler,
            init_method=init_method,
            scaled_init_method=scaled_init_method)

        if not self.add_ict_head:
            self.lm_head = BertLMHead(
                self.language_model.embedding.word_embeddings.weight.size(0),
                args.hidden_size, init_method, args.layernorm_epsilon, parallel_output)
            self._lm_head_key = 'lm_head'
        if self.add_binary_head:
            self.binary_head = get_linear_layer(args.hidden_size, 2,
                                                init_method)
            self._binary_head_key = 'binary_head'
        elif self.add_ict_head:
            self.ict_head = get_linear_layer(args.hidden_size, ict_head_size, init_method)
            self._ict_head_key = 'ict_head'

    def forward(self, input_ids, attention_mask, tokentype_ids=None):

        extended_attention_mask = bert_extended_attention_mask(
            attention_mask, next(self.language_model.parameters()).dtype)
        position_ids = bert_position_ids(input_ids)

        if self.add_binary_head or self.add_ict_head:
            lm_output, pooled_output = self.language_model(
                input_ids,
                position_ids,
                extended_attention_mask,
                tokentype_ids=tokentype_ids)
        else:
            lm_output = self.language_model(
                input_ids,
                position_ids,
                extended_attention_mask,
                tokentype_ids=tokentype_ids)

        # Output.
        if self.add_ict_head:
            ict_logits = self.ict_head(pooled_output)
            return ict_logits, None

        lm_logits = self.lm_head(
            lm_output, self.language_model.embedding.word_embeddings.weight)
        if self.add_binary_head:
            binary_logits = self.binary_head(pooled_output)
            return lm_logits, binary_logits

        return lm_logits, None


    def state_dict_for_save_checkpoint(self, destination=None, prefix='',
                                       keep_vars=False):
        """For easy load when model is combined with other heads,
        add an extra key."""

        state_dict_ = {}
        state_dict_[self._language_model_key] \
            = self.language_model.state_dict_for_save_checkpoint(
                destination, prefix, keep_vars)
        if not self.add_ict_head:
            state_dict_[self._lm_head_key] \
                = self.lm_head.state_dict_for_save_checkpoint(
                    destination, prefix, keep_vars)
        if self.add_binary_head:
            state_dict_[self._binary_head_key] \
                = self.binary_head.state_dict(destination, prefix, keep_vars)
        elif self.add_ict_head:
            state_dict_[self._ict_head_key] \
                = self.ict_head.state_dict(destination, prefix, keep_vars)
        return state_dict_


    def load_state_dict(self, state_dict, strict=True):
        """Customized load."""

        self.language_model.load_state_dict(
            state_dict[self._language_model_key], strict=strict)
        if not self.add_ict_head:
            self.lm_head.load_state_dict(
                state_dict[self._lm_head_key], strict=strict)
        if self.add_binary_head:
            self.binary_head.load_state_dict(
                state_dict[self._binary_head_key], strict=strict)
        elif self.add_ict_head:
            self.ict_head.load_state_dict(
                state_dict[self._ict_head_key], strict=strict)


class ICTBertModel(MegatronModule):
    def __init__(self,
                 num_layers,
                 vocab_size,
                 hidden_size,
                 num_attention_heads,
                 embedding_dropout_prob,
                 attention_dropout_prob,
                 output_dropout_prob,
                 max_sequence_length,
                 checkpoint_activations,
                 ict_head_size,
                 checkpoint_num_layers=1,
                 layernorm_epsilon=1.0e-5,
                 init_method_std=0.02,
                 num_tokentypes=0,
                 parallel_output=True,
                 apply_query_key_layer_scaling=False,
                 attention_softmax_in_fp32=False):

        super(ICTBertModel, self).__init__()
        bert_args = dict(
            num_layers=num_layers,
            vocab_size=vocab_size,
            hidden_size=hidden_size,
            num_attention_heads=num_attention_heads,
            embedding_dropout_prob=embedding_dropout_prob,
            attention_dropout_prob=attention_dropout_prob,
            output_dropout_prob=output_dropout_prob,
            max_sequence_length=max_sequence_length,
            checkpoint_activations=checkpoint_activations,
            add_binary_head=False,
            ict_head_size=ict_head_size,
            checkpoint_num_layers=checkpoint_num_layers,
            layernorm_epsilon=layernorm_epsilon,
            init_method_std=init_method_std,
            num_tokentypes=num_tokentypes,
            parallel_output=parallel_output,
            apply_query_key_layer_scaling=apply_query_key_layer_scaling,
            attention_softmax_in_fp32=attention_softmax_in_fp32)

        self.question_model = BertModel(**bert_args)
        self._question_key = 'question_model'
        self.context_model = BertModel(**bert_args)
        self._context_key = 'context_model'

    def forward(self, input_tokens, input_attention_mask, input_types,
                context_tokens, context_attention_mask, context_types):

        question_ict_logits, _ = self.question_model.forward(input_tokens, 1 - input_attention_mask, input_types)
        context_ict_logits, _ = self.context_model.forward(context_tokens, 1 - context_attention_mask, context_types)

        # [batch x h] * [h x batch]
        retrieval_scores = question_ict_logits.matmul(torch.transpose(context_ict_logits, 0, 1))

        return retrieval_scores

    def state_dict_for_save_checkpoint(self, destination=None, prefix='',
                                       keep_vars=False):
        state_dict_ = {}
        state_dict_[self._question_key] \
            = self.question_model.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars)
        state_dict_[self._context_key] \
            = self.context_model.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars)
        return state_dict_

    def load_state_dict(self, state_dict, strict=True):
        """Customized load."""

        self.question_model.load_state_dict(
            state_dict[self._question_key], strict=strict)
        self.context_model.load_state_dict(
            state_dict[self._context_key], strict=strict)
