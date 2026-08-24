"""Provider adapters for the native Router call core."""

from llmrouter_backend.adapters.fake import FakeAdapter
from llmrouter_backend.adapters.text import OllamaTextAdapter, OpenAITextAdapter

__all__ = ["FakeAdapter", "OllamaTextAdapter", "OpenAITextAdapter"]
