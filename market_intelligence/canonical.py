"""Canonical JSON and content identity for MarketIntelligence.

Re-exports the EvidenceSynthesis canonicalization, which is the Phase 7C serialization: ``sort_keys``, compact
separators, ``allow_nan=False`` and ASCII escaping. ``content_id(body) = "sha256:" + SHA-256(canonical body)``.
"""
from evidence_synthesis.canonical import canonical_json, content_id, to_plain

__all__ = ("canonical_json", "content_id", "to_plain")
