from typing import List, Optional
import torch
from experimaestro import Param
from xpmir.distributed import DistributableModel
from xpmir.letor.batchers import Batcher
from xpmir.neural import DualRepresentationScorer
from xpmir.rankers import Retriever
from xpmir.utils import easylog, foreach
from xpmir.text.encoders import TextEncoder
from xpmir.letor.context import Loss, TrainerContext, TrainingHook
from xpmir.letor.metrics import ScalarMetric

logger = easylog()


class DualVectorListener(TrainingHook):
    """Listener called with the (vectorial) representation of queries and
    documents

    The hook is called just after the computation of documents and queries
    representations.

    This can be used for logging purposes, but more importantly, to add
    regularization losses such as the :class:`FlopsRegularizer` regularizer.
    """

    def __call__(
        self, context: TrainerContext, queries: torch.Tensor, documents: torch.Tensor
    ):
        """Hook handler

        Args:
            context (TrainerContext): The training context
            queries (torch.Tensor): The query vectors
            documents (torch.Tensor): The document vectors

        Raises:
            NotImplementedError: _description_
        """
        raise NotImplementedError(f"__call__ in {self.__class__}")


class DualVectorScorer(DualRepresentationScorer):
    """A scorer based on dual vectorial representations"""

    pass


class Dense(DualVectorScorer):
    """A scorer based on a pair of (query, document) dense vectors"""

    encoder: Param[TextEncoder]
    """The document (and potentially query) encoder"""

    query_encoder: Param[Optional[TextEncoder]]
    """The query encoder (optional, if not defined uses the query_encoder)"""

    def __validate__(self):
        super().__validate__()
        assert not self.encoder.static(), "The vocabulary should be learnable"

    def _initialize(self, random):
        self.encoder.initialize()
        if self.query_encoder:
            self.query_encoder.initialize()

    def score_product(self, queries, documents, info: Optional[TrainerContext]):
        return queries @ documents.T

    def score_pairs(self, queries, documents, info: TrainerContext):
        scores = (queries.unsqueeze(1) @ documents.unsqueeze(2)).squeeze(-1).squeeze(-1)

        # Apply the dual vector hook
        if info is not None:
            foreach(
                info.hooks(DualVectorListener),
                lambda hook: hook(info, queries, documents),
            )
        return scores

    @property
    def _query_encoder(self):
        return self.query_encoder or self.encoder


class DenseBaseEncoder(TextEncoder):
    """A text encoder adapter for dense scorers (either query or document encoder)"""

    scorer: Param[Dense]

    def initialize(self):
        self.scorer.initialize(None)


class DenseDocumentEncoder(DenseBaseEncoder):
    @property
    def dimension(self):
        """Returns the dimension of the representation"""
        return self.scorer.encoder.dimension

    def forward(self, texts: List[str]) -> torch.Tensor:
        """Returns a matrix encoding the provided texts"""
        return self.scorer.encode_documents(texts)


class DenseQueryEncoder(DenseBaseEncoder):
    @property
    def dimension(self):
        """Returns the dimension of the representation"""
        return self.scorer._query_encoder.dimension

    def forward(self, texts: List[str]) -> torch.Tensor:
        """Returns a matrix encoding the provided texts"""
        return self.scorer.encode_queries(texts)


class CosineDense(Dense):
    """Dual model based on cosine similarity."""

    def encode_queries(self, texts):
        queries = (self.query_encoder or self.encoder)(texts)
        return queries / queries.norm(dim=1, keepdim=True)

    def encode_documents(self, texts):
        documents = self.encoder(texts)
        return documents / documents.norm(dim=1, keepdim=True)


class DotDense(Dense, DistributableModel):
    """Dual model based on inner product."""

    def __validate__(self):
        super().__validate__()
        assert not self.encoder.static(), "The vocabulary should be learnable"

    def encode_queries(self, texts: List[str]):
        """Encode the different queries"""
        return self._query_encoder(texts)

    def encode_documents(self, texts: List[str]):
        """Encode the different documents"""
        return self.encoder(texts)

    def getRetriever(
        self, retriever: "Retriever", batch_size: int, batcher: Batcher, device=None
    ):
        from xpmir.rankers.full import FullRetrieverRescorer

        return FullRetrieverRescorer(
            documents=retriever.documents,
            scorer=self,
            batchsize=batch_size,
            batcher=batcher,
            device=device,
        )

    def distribute_models(self, update):
        self.encoder.model = update(self.encoder.model)
        self.query_encoder.model = update(self.query_encoder.model)


class FlopsRegularizer(DualVectorListener):
    r"""The FLOPS regularizer computes

    .. math::

        FLOPS(q,d) = \lambda_q FLOPS(q) + \lambda_d FLOPS(d)

    where

    .. math::
        FLOPS(x) = \left( \frac{1}{d} \sum_{i=1}^d |x_i| \right)^2
    """
    lambda_q: Param[float]
    """Lambda for queries"""

    lambda_d: Param[float]
    """Lambda for documents"""

    @staticmethod
    def compute(x: torch.Tensor):
        # Computes the mean for each term
        y = x.abs().mean(0)
        # Returns the sum of squared means
        return y, (y * y).sum()

    def __call__(self, info: TrainerContext, queries, documents):
        # queries and documents are length x dimension
        # Assumes that all weights are positive
        assert info.metrics is not None

        # q of shape (dimension), flops_q of shape (1)
        q, flops_q = FlopsRegularizer.compute(queries)
        d, flops_d = FlopsRegularizer.compute(documents)

        flops = self.lambda_d * flops_d + self.lambda_q * flops_q
        info.add_loss(Loss("flops", flops, 1.0))

        info.metrics.add(ScalarMetric("flops", flops.item(), len(q)))
        info.metrics.add(ScalarMetric("flops_q", flops_q.item(), len(q)))
        info.metrics.add(ScalarMetric("flops_d", flops_d.item(), len(d)))

        with torch.no_grad():
            info.metrics.add(
                ScalarMetric(
                    "sparsity_q",
                    (queries != 0).sum().item() / (queries.shape[0] * queries.shape[1]),
                    len(q),
                )
            )
            info.metrics.add(
                ScalarMetric(
                    "sparsity_d",
                    (documents != 0).sum().item()
                    / (documents.shape[0] * documents.shape[1]),
                    len(d),
                )
            )
