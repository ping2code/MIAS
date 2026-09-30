"""Options data (Phase 9): provider records -> an immutable, cutoff-aware OptionsSnapshot.

- Phase 9A: ``config`` and ``massive_check``, a user-run, read-only Massive Options contract check (I/O).
- Phase 9B: the pure snapshot layer: ``model``, ``identity``, ``normalization`` (assembly), ``validation`` and
  ``canonical``. No network, files, environment, clock, database or AI.

The live adapter and runner (Phase 9C) and OptionsIntelligence (Phase 9D) are not implemented yet. See
docs/phase9a-massive-options-check.md and docs/phase9b-options-snapshot.md.
"""
