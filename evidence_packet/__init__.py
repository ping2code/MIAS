"""Phase 7C v1 Unified Evidence Packet: typed, immutable transport of independent evidence (it concludes nothing).

The pure assembler (``evidence_packet.assembler.assemble``) receives already-produced
evidence and performs no I/O: no market data, technical engine, news collection,
database, Redis, AI or clock.

This package never imports ``collector``, ``analyzer``, ``alert_engine``,
``shared.config``, ``evidence`` or ``evaluation``.
"""
