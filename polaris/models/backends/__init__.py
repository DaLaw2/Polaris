"""Tagger backends.

Adding a model: upload a definition and build a version of it.
"""

from .base import ALL_CATEGORIES, BackendConfig, TaggerBackend

__all__ = [
    "ALL_CATEGORIES",
    "BackendConfig",
    "TaggerBackend",
]
