"""Options data (Phase 9): provider records -> an immutable, cutoff-aware OptionsSnapshot.

- Phase 9A: ``config`` and ``massive_check``, a user-run, read-only Massive Options contract check (I/O).
- Phase 9B: the pure snapshot layer: ``model``, ``identity``, ``normalization`` (assembly), ``validation`` and
  ``canonical``. No network, files, environment, clock, database or AI.

- Phase 9C: ``provider`` (the provider-neutral interface and a fixture provider), ``massive`` (the live chain
  adapter) and ``runner`` (one clock read, pure assembly, atomic canonical snapshot file). I/O modules.

OptionsIntelligence (Phase 9D) is not implemented yet. See docs/phase9a-massive-options-check.md,
docs/phase9b-options-snapshot.md and docs/phase9c-massive-options-adapter.md.
"""
