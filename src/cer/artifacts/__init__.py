"""CER filesystem artifact store package.

Exposes :class:`~cer.artifacts.fs_store.FilesystemArtifactStore`, the sole
concrete implementation of :class:`cer.contract.stores.ArtifactStore`
provided by this package (a hash-addressed local-filesystem backend). See
``fs_store.py`` for the on-disk layout and full semantics.
"""

from __future__ import annotations

from .fs_store import FilesystemArtifactStore

__all__ = ["FilesystemArtifactStore"]
