# MIAS operational market cycle

The operational wrapper uses the implemented runners and frozen contracts. No analytical rules, Phase 6 artifacts, schema, or Redis state are changed.

## Flow

1. Refresh META/NVDA daily, hourly and five-minute technical snapshots with massive_stocks, regular sessions, and the existing phase4c-v2 shadow writer. Phase 6 evidence ledger collection is explicitly disabled.
2. Require six persisted/duplicate snapshots, zero failure/drop/conflict counters and a drained writer. Verify stored content hashes and completed-bar freshness in a database-enforced read-only transaction.
3. Generate Market Context and full options chains (250 records/page, 60 pages, 80 requests maximum). Stop if a chain remains truncated.
4. Build strict Evidence Packets, Synthesis, Market Intelligence and phase9-v2 Options Intelligence at matching per-symbol as-of times. Apply the existing sealed live-validation Trade Setup policy.
5. For prior published setup_candidates assessments, check later intelligence through the existing invalidation interface. Never invalidate a no_setup assessment. Earlier invalidated checks remain terminal.
6. Validate the entire batch before publishing. Scale the designated publisher from zero to one; copy only into its /tmp; publish through artifact_store.runner; verify the store; scale back to zero in finally; verify each artifact through the authenticated API.
7. Record a published manifest only after API verification succeeds.

No_setup is a complete valid result. It is not a validation error or an instruction to loosen policy.

## Schedule and operation

The user systemd timer wakes every 30 minutes. The wrapper runs only on exchange trading days from 08:30 through 16:30 America/New_York; it skips other times. The machine, WSL, network, existing provider credentials and OpenShift login must remain available. The timer does not wake a powered-off or suspended Windows machine.

Credentials are loaded from the existing /home/pentu/.mias-env without printing their values. The stocks delay remains the configured value (default 900 seconds); no options delay is invented. This is delayed stock evidence, not a claim of real-time stock data.

Runs and receipts live under operations/runs/ and are ignored by Git. A file lock prevents overlapping cycles. On any operational or validation failure, operations/HALTED.json is written; subsequent scheduled runs refuse to proceed. There is no automatic retry after a validation failure.

Inspect:
- systemctl --user status mias-operational.timer mias-operational.service
- journalctl --user -u mias-operational.service
- operations/HALTED.json, if present
- the latest validated-manifest.json and published-manifest.json

Investigate a halted run before clearing its marker. If publishing was interrupted, reconcile the sole-writer store and API first; individual artifacts are immutable and publishing is idempotent, but a batch is not transactionally atomic across artifacts.

Manual full run after review:
source /home/pentu/.mias-env
.venv/bin/python scripts/mias_operational_cycle.py

Disable scheduled runs:
systemctl --user disable --now mias-operational.timer

The existing scripts/validate_massive.py is user-owned and was preserved. The dashboard placeholder cleanup is deferred: changing the live signed UI image is not required for pipeline correctness.
