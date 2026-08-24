"""Provider adapters for the native Router call core."""

from llmrouter_backend.adapters.composite import CompositeProviderAdapter
from llmrouter_backend.adapters.embedding import (
    OllamaEmbeddingAdapter,
    OpenAIEmbeddingAdapter,
)
from llmrouter_backend.adapters.fake import FakeAdapter
from llmrouter_backend.adapters.local_embedding import (
    LocalEmbeddingAdapter,
    LocalEmbeddingConfiguration,
)
from llmrouter_backend.adapters.text import OllamaTextAdapter, OpenAITextAdapter
from llmrouter_backend.adapters.wavespeed import WaveSpeedMediaAdapter

__all__ = [
    "CompositeProviderAdapter",
    "FakeAdapter",
    "LocalEmbeddingAdapter",
    "LocalEmbeddingConfiguration",
    "OllamaEmbeddingAdapter",
    "OllamaTextAdapter",
    "OpenAIEmbeddingAdapter",
    "OpenAITextAdapter",
    "WaveSpeedMediaAdapter",
]
