# Phase 12E: Alert runner, receipts and recovery

Phase 12E connects the sealed Phase 12 pieces into explicit operator commands: 12B (AlertEvent), 12C (alert state), and
12D (render, delivery guard, Telegram adapter). It adds no rule, state or render semantics. The locked contracts are
unchanged:

- AlertEvent `phase12-v1`, with rules `phase12-rules-v1` and pointers `phase12-pointer-v1`;
- state `phase12-state-v1` (`new` / `duplicate` / `terminal_suppressed`; precedence duplicate → terminal → new);
- render `phase12-render-v1`;
- delivery `phase12-delivery-v1` (`delivered` / `failed`, with closed error codes).

Entry point: `python -m alert_engine.runner <command>`. Modules: `alert_engine/runner.py` and
`alert_engine/receipts.py`.

## Authorities

| Question | Authority | Cache / evidence |
|---|---|---|
| What happened? | Sealed AlertEvent files (12B) | none |
| Was an alert accepted, and is a setup terminal? | Pure replay of an **ordered** list of sealed AlertEvents (12C `replay`) | Redis `seen` and `subject` keys |
| Was it delivered? | Delivery receipts (12E), append-only files | Redis `delivered` marker (12D guard) |

Redis is never the historical authority. Phase 12C state is never rebuilt from receipts, and receipts never alter an
AlertEvent or its id.

## Commands

| Command | Reads | Writes | Network |
|---|---|---|---|
| `build` | one sealed source: `--assessment`, `--invalidation-check`, or `--previous-market-intelligence` + `--current-market-intelligence` | one AlertEvent file (atomic; `--overwrite` to replace) | none |
| `replay` | ordered `--alert` files or a `--manifest` | optional replay document | none |
| `restore-state --confirm` | ordered alerts | missing Phase 12C keys only | Redis |
| `restore-delivery --confirm` | the receipt directory | missing delivered markers only | Redis |
| `run-once` | one AlertEvent | 12C commit, guard keys, one receipt per send | Redis, Telegram |
| `deliver` | one *accepted* AlertEvent | guard keys, one receipt per send | Redis, Telegram |
| `verify-alert` | alert + its sealed sources | nothing | none |
| `verify-replay` | replay document + ordered alerts | nothing | none |
| `verify-receipts` | receipt directory + alerts | nothing | none |

**build:** when the rule doesn't fire, the summary is `{"result": "NO_ALERT"}`, exit 0, and no file is written. There's
never a placeholder event. A built event is verified by re-derivation before it's written. The write goes to a
temporary file, is fsynced, then hard-linked into place (no-clobber).

**replay:** the order is exactly the order of the `--alert` arguments, or of the manifest's `alerts` list. It's never
sorted, by name or by any clock. The manifest format is:

```json
{"manifest_format_version": "phase12-replay-manifest-v1", "alerts": ["a1.json", "a2.json"]}
```

Paths are relative to the manifest's directory. The replay document (`phase12-replay-v1`) holds the input `alert_ids`,
the per-event decisions and the final state. It's deterministic, so `verify-replay` compares bytes.

**run-once** (live, for exactly one alert):

1. Check the preconditions before touching any state: a valid AlertEvent, a writable receipt directory with valid
   history, Telegram credentials present, and the Redis URL present.
2. Classify and commit with the Phase 12C store (`record`).
3. If `duplicate` or `terminal_suppressed`: stop with exit 0, no render and no send.
4. If `new`: render (`delivery_request`), then `RedisDeliveryGuard.deliver`. On a provider send the adapter result is
   written as a receipt, **before** the guard records the delivered marker.

**deliver** (delivery-only retry): an accepted alert isn't necessarily a delivered alert. After a failed send, a
second `run-once` is a `duplicate` and won't send, so `deliver` retries without needing `new`. It requires the alert's
12C `seen` marker (exit 2 otherwise), so it can never bypass dedupe or terminal suppression. The guard still prevents a
re-send once the alert is delivered (`already_delivered`, exit 0).

There's no polling, daemon or directory watching: each invocation handles exactly what the operator names.

## Receipts (`phase12-receipt-v1`)

Each provider send produces one file, `<channel>-<alert hex>-<sequence:06d>.json`, in the operator-provided
`--receipt-dir`. The fields form a closed set:

- **Identity:** `receipt_format_version`, `alert_id`, `channel`, `sequence`.
- **Outcome:** `status` (`delivered` | `failed`); `provider_message_id` (delivered only); `attempts` (provider calls in
  this send, including the adapter's bounded retries); `safe_error_code` (failed only, from the closed 12D codes).
- **Timing:** `attempted_at` and `completed_at`, runtime UTC that never feeds any decision.
- **Versions:** `delivery_contract_version`, `render_version`.

A receipt holds no token, chat id, URL, header, raw response, exception text or rendered text.

- **Append-only:** each receipt is written to a temporary file, fsynced, then linked into place no-clobber. An existing
  receipt is never replaced. A concurrent writer that loses a sequence number takes the next one.
- **Ordering:** receipts are ordered by the `sequence` in each file, never by directory listing order.
- **Fail closed:** a gap in the sequence, a name that disagrees with the content, an unknown file, duplicate JSON keys,
  or a malformed field all fail closed. A leftover `.receipt-*.tmp` from an interrupted write is ignored.
- **Not delivery state:** `already_delivered` and `in_progress` make no provider call, so they write no receipt.

## Failure model and exit codes

| Exit | Meaning |
|---|---|
| 0 | Success or a valid no-op: `NO_ALERT`, `duplicate`, `terminal_suppressed`, `already_delivered`. |
| 2 | Usage or invalid input; missing credentials or URL; `deliver` on an unaccepted alert; `restore-*` without `--confirm`. |
| 3 | Alert or delivery state unavailable or conflicting; another sender holds the delivery lease (`in_progress`). |
| 4 | Delivery failed. A `failed` receipt is written, and the alert stays retryable with `deliver`. |
| 5 | Receipt or output storage failure (including malformed receipt history, or an existing output without `--overwrite`). |

What happens in each failure case:

- **Redis down before the send:** exit 3, nothing is sent and no receipt is written.
- **Confirmed send, but the delivered marker can't be written:** exit 3. The guard keeps its lease until the TTL
  expires, so no immediate re-send happens. The `delivered` receipt already exists, so `restore-delivery` can rebuild
  the marker without sending again.
- **Confirmed send, but the receipt can't be written:** exit 5. The marker is still recorded, so there's no re-send.
- **At-least-once:** a crash between Telegram accepting a message and the marker write can still cause one duplicate
  send after the lease TTL. Two `delivered` receipts then exist for one alert; `verify-receipts` reports them as
  `duplicate_sends`, and `restore-delivery` treats either message id as consistent.

Every summary is one line of JSON: ids, codes, counts, classification, delivery action and status. It never contains
secrets, URLs or rendered text. Failures go to stderr in the same shape, with `result: FAILED` and an `error`.

## Recovery after a Redis loss

1. `replay` the ordered sealed history and check it (`verify-replay`).
2. `restore-state --confirm` with the same ordered alerts. This writes only missing `seen` and `subject` keys; any
   disagreement writes nothing and exits 3.
3. `restore-delivery --confirm --receipt-dir DIR` rebuilds only missing `delivered` markers, and only from `delivered`
   receipts. An existing marker must equal a message id a receipt proves; otherwise nothing is written and it exits 3.
   Failed-only alerts get no marker, so they stay retryable.
4. Retry any undelivered accepted alert with `deliver`.

No restore flushes, scans keys, or overwrites conflicting keys. Both restores are single-writer recovery steps: run them
while no live `run-once` or `deliver` is active.

## Configuration and secrets

- **Redis:** the URL comes from the process environment variable named by `--redis-url-env` (default
  `MIAS_ALERT_REDIS_URL`) and is never printed. `--namespace` defaults to `mias:phase12`.
- **Telegram:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` come from the process environment only. They're never
  read from `.env` and never printed.
- **Imports:** importing the runner loads neither `redis` nor `requests`. `build`, `replay` and the `verify-*`
  commands never load them.

## Scope decisions

- **Orchestrator wiring:** deferred. The runner is complete as an operator-invoked command, and wiring it into the
  scheduler would couple Phase 12 to orchestration without a closure need. A future step can call `run-once` per
  sealed alert. It would stay disabled by default, with no directory polling.
- **PostgreSQL:** not used. Sealed files, receipts and the replay are sufficient, and the migration head stays
  `0007_technical_evidence_ledger`.
- **Legacy paths:** `alert_engine/telegram_notifier.py`, `formatter.py` and `decision_engine.py` are untouched and
  not used by the runner.
