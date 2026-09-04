"""End-to-end integration proof suite for CER W3-PROOF.

Everything in this package is driven through the real HTTP API
(``fastapi.testclient.TestClient`` over ``cer.api.app.create_app``) wired
to the REAL ``cer.metadata.SQLiteMetadataStore`` and REAL
``cer.artifacts.FilesystemArtifactStore`` backends on real temporary
filesystem paths -- never the in-memory fakes in ``tests/api/fakes.py``.

The component-level suites (``tests/api``, ``tests/metadata``,
``tests/artifacts``, ``tests/contract``, ``tests/runtime``) already prove
each layer in isolation, several of them against fakes. This package exists
because a defect can be invisible to every component suite when the fakes
disagree with the real store in some subtle way -- the API's behaviour is
only actually proven once it's driven through the real stores end to end.
Each module here maps directly onto one of the PID's acceptance criteria;
see the module docstring in each file for which one.
"""

from __future__ import annotations
