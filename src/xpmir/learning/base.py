from abc import ABC, abstractmethod
from typing import Optional, Generic, TypeVar
import numpy as np
from functools import cached_property
from experimaestro import Config, Param
from xpmir.utils.utils import EasyLogger
from xpmir.utils.iter import SerializableIterator, BatchIteratorAdapter


class Random(Config):
    """Random configuration"""

    seed: Param[int] = 0
    """The seed to use so the random process is deterministic"""

    @cached_property
    def state(self) -> np.random.RandomState:
        return np.random.RandomState(self.seed)

    def __getstate__(self):
        return {"seed": self.seed}


class Sampler(Config, EasyLogger):
    """Abstract data sampler"""

    def initialize(self, random: Optional[np.random.RandomState]):
        self.random = random or np.random.RandomState(random.randint(0, 2**31))


T = TypeVar("T")


class BaseSampler(Sampler, Generic[T], ABC):
    @abstractmethod
    def __iter__() -> SerializableIterator[T]:
        pass

    def __batch_iter__(self, batch_size: int) -> BatchIteratorAdapter:
        return BatchIteratorAdapter(self.__iter__(), batch_size)
