from typing import List
from experimaestro import Param
from .encoders import TokenizedTextEncoderBase, InputType, RepresentationOutput


class MeanTextEncoder(TokenizedTextEncoderBase[InputType, RepresentationOutput]):
    """Returns the mean of the word embeddings"""

    encoder: Param[TokenizedTextEncoderBase[InputType, RepresentationOutput]]

    def __initialize__(self, options):
        self.encoder.__initialize__(options)

    def static(self):
        return self.encoder.static()

    @property
    def dimension(self):
        return self.encoder.dimension

    def forward(self, texts: List[InputType], options=None) -> RepresentationOutput:
        emb_texts = self.encoder(texts, options=options)
        # Computes the mean over the time dimension (vocab output is batch x time x dim)
        return emb_texts.value.mean(1)
