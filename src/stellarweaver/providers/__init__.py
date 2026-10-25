"""Explicit model adapters."""

from stellarweaver.providers.base import ModelProvider, ProviderError
from stellarweaver.providers.fake import FakeModel

__all__ = ["FakeModel", "ModelProvider", "ProviderError"]
