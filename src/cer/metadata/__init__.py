"""CER metadata storage backends.

Currently exposes :class:`SQLiteMetadataStore`, the SQLite implementation
of :class:`cer.contract.stores.MetadataStore`.
"""

from __future__ import annotations

from .sqlite_store import SQLiteMetadataStore

__all__ = ["SQLiteMetadataStore"]
