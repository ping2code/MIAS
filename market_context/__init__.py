"""Phase 7B.1 Market Context: deterministic, provider-independent market-context facts (no signals, no labels).

The pure engine (``market_context.engine``) consumes completed 5m ``MarketBar``
sequences with an explicit ``now``, ``as_of`` and ``ExchangeCalendar``. Provider
fetching lives only in ``market_context.runner``.

This package never imports ``evaluation`` or ``evidence``, and never persists
anything.
"""
