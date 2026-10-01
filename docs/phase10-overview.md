# Phase 10 — Trade Setup: overview and closure status

**Status:** 10A–10D are complete. 10E's implementation is complete, but its **live-session validation is pending**.
**Phase 10 is not yet closed** (see the checklist in `docs/phase10e-runner-live-validation.md` §9).

Phase 10 turns sealed MarketIntelligence (Phase 8) and OptionsIntelligence (Phase 9) into a deterministic
`TradeSetupAssessment`, using an explicit policy. It checks the setup's market state later with a separate
`InvalidationCheck`.

The scope is long single-leg calls and puts only. There is no ranking, "best" contract, score, sizing, target, stop,
reward/risk, execution or AI.

```
MarketIntelligence (phase8-v1) ─┐
OptionsIntelligence (phase9-v2) ─┼─► assess ─► TradeSetupAssessment (phase10-v1)
Policy (phase10-policy-v1) ──────┘                     │
                    later MarketIntelligence ──────────┴─► check_invalidation ─► InvalidationCheck
```

| Step | What it added | Document |
|---|---|---|
| 10A | Architecture freeze (D1–D12, R1–R5) | (frozen decisions recorded in the 10B–10D documents) |
| 10B | Policy, input validation, market bias, global gates, `PreScreeningEligibility`, phase9-v2 amendment | `phase10b-trade-setup-core.md` |
| 10C | Contract screening, candidates, aggregated rejections, premium A/B/C, delta bound | `phase10c-contract-screening.md` |
| 10D | `InvalidationCheck`, validator hardening, risk-fact decisions | `phase10d-invalidation.md` |
| 10E | `trade_setup.runner`, replay fixtures, live-validation tooling and policy | `phase10e-runner-live-validation.md` |

**Frozen versions:**
- assessment: `phase10-v1`, rules `phase10-rules-v1`, policy `phase10-policy-v1`, pointer `phase10-pointer-v1`;
- invalidation: `phase10-invalidation-v1`, rules `phase10-invalidation-rules-v1`, pointer
  `phase10-invalidation-pointer-v1`;
- OptionsIntelligence `phase9-v1` and `phase9-v2` are both supported.

**Boundaries:**
- `trade_setup` is pure, except `trade_setup/runner.py`, which reads and writes local files.
- Collection (`options_data.runner`, `market_context.runner`, `evidence_packet.runner`) stays outside it and is
  operator-run.
- Phase 6 is untouched.
