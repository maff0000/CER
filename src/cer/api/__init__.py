"""CER HTTP API — the FastAPI service producers/consumers talk to.

This package implements no storage of its own: it is a thin HTTP surface
over a :class:`cer.contract.stores.MetadataStore` and a
:class:`cer.contract.stores.ArtifactStore`, both supplied by the caller of
:func:`cer.api.app.create_app` (dependency injection — the real backends
are wired by the PL at integration; this package's own tests use in-memory
fakes that satisfy the Protocols).
"""

from __future__ import annotations
