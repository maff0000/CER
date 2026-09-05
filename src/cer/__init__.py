"""CER — Canonical Evidence Registry.

CER is the canonical empirical evidence and lineage system: it records
which strategy/version evidence belongs to, which experiment/run produced
it, which code commit/dataset/configuration/environment produced it, which
producer wrote it, which metrics/verdicts were recorded, where associated
artifacts live, and what promotion/strategy-health history exists.

This package provides the versioned contract layer (``cer.contract``):
pure-domain identity, enums, error types, models and storage interfaces
that every other CER component (HTTP service, storage backends, producer
clients) is built against. It intentionally implements no storage, no
HTTP, and no I/O of its own.
"""

__version__ = "1.0.0"
