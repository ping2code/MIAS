"""Immutable, content-addressed storage for sealed MIAS analytical objects (``artifact-store-v1``). Framework-free.

- ``kinds``: the closed kinds and their domain validators;
- ``layout`` and ``files``: safe paths and symlink-proof file access;
- ``store``: parsing, validation and the no-clobber publish;
- ``index``: the validated, immutable in-memory index and its queries;
- ``runner``: the operator CLI (``publish``, ``verify``).

See ``docs/phase13c-artifact-read-api.md``.
"""
