"""Phase 12D delivery boundary: the provider-neutral contract (``base``), the hardened Telegram adapter
(``telegram``) and the Redis delivery lease and marker (``guard``). All delivery data is runtime metadata, never part of
an AlertEvent or of Phase 12C state."""
