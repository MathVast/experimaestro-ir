from typing import List, Optional, Tuple
from experimaestro import Config, Param
import torch.nn as nn
import torch
from experimaestro import initializer
from xpmir.distributed import DistributableModel
from xpmir.letor.samplers import PairwiseSampler, PairwiseInBatchNegativesSampler
from transformers import AutoModelForMaskedLM
from xpmir.text.huggingface import TransformerVocab
from xpmir.text.encoders import TextEncoder
from xpmir.letor.trainers.batchwise import BatchwiseTrainer
from xpmir.neural.dual import DotDense, ScheduledFlopsRegularizer
from xpmir.utils import easylog

logger = easylog()


class Aggregation(Config):
    """The aggregation function for Splade"""

    pass


class MaxAggregation(Aggregation):
    """Aggregate using a max"""

    def __call__(self, logits, mask):
        values, _ = torch.max(
            torch.log1p(torch.relu(logits) * mask.to(logits.device).unsqueeze(-1)),
            dim=1,
        )
        return values


class SumAggregation(Aggregation):
    """Aggregate using a sum"""

    def __call__(self, logits, mask):
        return torch.sum(
            torch.log1p(torch.relu(logits) * mask.to(logits.device).unsqueeze(-1)),
            dim=1,
        )


class SpladeTextEncoderModel(nn.Module):
    def __init__(self, encoder, aggregation):
        super().__init__()
        self.encoder = encoder
        self.aggregation = aggregation

    def forward(self, tokenized):
        # We stock all the outputs in order to get the embedding matrix
        # Here as the automodel is not the same as the normal AutoModel,
        # So here the output has the attribute logits, the w_ij in the paper
        # which is of shape (bs, len(texts), vocab_size)
        out = self.encoder(tokenized, all_outputs=True)
        out = self.aggregation(out.logits, tokenized.mask)
        return out


class SpladeTextEncoder(TextEncoder, DistributableModel):
    """Splade model

    It is only a text encoder since the we use `xpmir.neural.dual.DotDense`
    as the scorer class
    """

    encoder: Param[TransformerVocab]
    """The encoder from Hugging Face"""

    aggregation: Param[Aggregation]
    """How to aggregate the vectors"""

    maxlen: Param[Optional[int]] = None
    """Max length for texts"""

    @initializer
    def initialize(self):
        self.encoder.initialize(automodel=AutoModelForMaskedLM)
        self.model = SpladeTextEncoderModel(self.encoder, self.aggregation)

    def forward(self, texts: List[str]) -> torch.Tensor:
        """Returns a batch x vocab tensor"""
        tokenized = self.encoder.batch_tokenize(texts, mask=True, maxlen=self.maxlen)
        out = self.model(tokenized)
        return out

    @property
    def dimension(self):
        return self.encoder.model.config.vocab_size

    def static(self):
        return False

    def distribute_models(self, update):
        self.model = update(self.model)


def _splade(
    lambda_q: float,
    lambda_d: float,
    aggregation: Aggregation,
    min_lambda_q: float = 0,
    min_lambda_d: float = 0,
    lamdba_warmup_steps: int = 0,
):
    # Unlike the cross-encoder, here the encoder returns the whole last layer
    # In the paper we use the DistilBERT-based as the checkpoint
    encoder = TransformerVocab(model_id="distilbert-base-uncased", trainable=True)

    # make use the output of the BERT and do an aggregation
    doc_encoder = SpladeTextEncoder(
        aggregation=aggregation, encoder=encoder, maxlen=200
    )
    query_encoder = SpladeTextEncoder(
        aggregation=aggregation, encoder=encoder, maxlen=30
    )

    return DotDense(
        encoder=doc_encoder, query_encoder=query_encoder
    ), ScheduledFlopsRegularizer(
        lambda_q=lambda_q,
        lambda_d=lambda_d,
        min_lambda_q=min_lambda_q,
        min_lambda_d=min_lambda_d,
        lamdba_warmup_steps=lamdba_warmup_steps,
    )


def spladeV1(lambda_q: float, lambda_d: float, lamdba_warmup_steps: int = 0):
    """Returns the Splade architecture"""
    return _splade(lambda_q, lambda_d, SumAggregation(), lamdba_warmup_steps)


def spladeV2(
    lambda_q: float,
    lambda_d: float,
    min_lambda_q: float = 0,
    min_lambda_d: float = 0,
    lamdba_warmup_steps: int = 0,
):
    """Returns the Splade v2 architecture

    SPLADE v2: Sparse Lexical and Expansion Model for Information Retrieval
    (arXiv:2109.10086)
    """
    return _splade(
        lambda_q,
        lambda_d,
        MaxAggregation(),
        min_lambda_q,
        min_lambda_d,
        lamdba_warmup_steps,
    )


def spladev2___(sampler: PairwiseSampler) -> Tuple[BatchwiseTrainer, DotDense]:
    """Returns the model described in Splade V2"""
    from xpmir.letor.optim import Adam
    from xpmir.letor.trainers.batchwise import SoftmaxCrossEntropy

    ibn_sampler = PairwiseInBatchNegativesSampler(sampler=sampler)
    trainer = BatchwiseTrainer(
        batch_size=124,
        optimizer=Adam(lr=2e-5),
        sampler=ibn_sampler,
        lossfn=SoftmaxCrossEntropy(),
    )

    # Trained with distillation
    return trainer, spladeV2()
