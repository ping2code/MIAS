"""Research-only technical configurations for Phase 4D evaluation (never used by the live runner).

``research_config(pivot_window)`` returns a ``TechnicalConfig`` identical to the
production default except for ``pivot_window``. In Phase 4D, **only**
``pivot_window`` may vary. Every other threshold is the production value: RSI zones,
breakout buffer, relative-volume threshold, confidence rules and level clustering.

- The live runner (``technical.runner``) and the persistence layer never import this
  module, and production uses ``TechnicalConfig()`` (pivot window 2) unchanged. The
  tests assert both.
- Evaluation output carries ``research_config: true`` and the pivot window whenever
  a research configuration is used, so results are never mistaken for production.
"""
from dataclasses import replace

from technical.models import TechnicalConfig

PRODUCTION_CONFIG = TechnicalConfig()
RESEARCH_PIVOT_WINDOWS = (1, 2, 3, 4)
MAX_RESEARCH_PIVOT_WINDOW = 10


class ResearchConfigError(ValueError):
    """An invalid or disallowed research configuration."""


def research_config(pivot_window):
    if isinstance(pivot_window, bool) or not isinstance(pivot_window, int) or \
            not 1 <= pivot_window <= MAX_RESEARCH_PIVOT_WINDOW:
        raise ResearchConfigError(f"research pivot window must be an integer 1-{MAX_RESEARCH_PIVOT_WINDOW}")
    return replace(PRODUCTION_CONFIG, pivot_window=pivot_window)


def is_production(config):
    return config == PRODUCTION_CONFIG


def differing_fields(config):
    """Fields that differ from production (for the research guard and the output metadata)."""
    return sorted(name for name in PRODUCTION_CONFIG.__dataclass_fields__
                  if getattr(config, name) != getattr(PRODUCTION_CONFIG, name))
