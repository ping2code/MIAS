"""Phase 12E alert runner: explicit, operator-driven commands around the sealed Phase 12 pipeline. The only alert
module that reads files, the process environment or the clock.

    python -m alert_engine.runner build (--assessment A.json | --invalidation-check C.json |
        --previous-market-intelligence P.json --current-market-intelligence Q.json) --output ALERT.json [--overwrite]
    python -m alert_engine.runner replay (--alert ALERT.json ... | --manifest M.json) [--output REPLAY.json]
        [--overwrite]
    python -m alert_engine.runner restore-state (--alert ... | --manifest M.json) --confirm [redis options]
    python -m alert_engine.runner restore-delivery --receipt-dir DIR --confirm [redis options]
    python -m alert_engine.runner run-once --alert ALERT.json --receipt-dir DIR [--channel telegram] [redis options]
    python -m alert_engine.runner deliver --alert ALERT.json --receipt-dir DIR [--channel telegram] [redis options]
    python -m alert_engine.runner verify-alert --alert ALERT.json (source options as for build)
    python -m alert_engine.runner verify-replay --replay REPLAY.json (--alert ... | --manifest M.json)
    python -m alert_engine.runner verify-receipts --receipt-dir DIR (--alert ... | --manifest M.json)

Redis options: ``--redis-url-env NAME`` (default ``MIAS_ALERT_REDIS_URL``; the URL is read from that process
environment variable and never printed) and ``--namespace`` (default ``mias:phase12``). Telegram credentials come from
the process environment only (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``): never ``.env``, never printed.

- **build** applies one Phase 12B rule to sealed inputs and verifies the event by re-derivation before writing it
  atomically (temporary file, fsync, then a no-clobber hard link, or a replace with ``--overwrite``). When the rule
  doesn't fire, the result is an explicit ``NO_ALERT`` and nothing is written. No Redis, Telegram or clock.
- **replay** folds an explicit, ordered list of sealed AlertEvents (``--alert`` in the given order, or a manifest's
  ``alerts`` list in its order; never sorted) through the pure Phase 12C ``replay``. It's the recovery authority for
  alert state. No Redis, Telegram, configuration or network. ``--output`` writes the deterministic replay document.
- **restore-state** (explicit ``--confirm``) writes that replay into Phase 12C Redis keys with ``restore``: exact keys
  only, missing keys only; any disagreement writes nothing and fails closed. No flush, no key scan.
- **restore-delivery** (explicit ``--confirm``) rebuilds missing Phase 12D delivered markers from ``delivered``
  receipts only. An existing marker must equal a message id that a delivered receipt proves; otherwise nothing is
  written. Malformed receipt history fails closed. No Telegram, and Phase 12C state is never rebuilt from receipts.
- **run-once** processes exactly one sealed AlertEvent: validate, classify and commit with Phase 12C. ``duplicate`` and
  ``terminal_suppressed`` stop there (no render, no send). ``new`` is rendered, sent through the Phase 12D delivery
  guard, and a receipt is written for the provider send.
- **deliver** retries delivery for an alert Phase 12C has *already accepted* (its seen marker exists). It doesn't need
  the alert to be ``new``: acceptance is not delivery. The guard still prevents a re-send once delivered.
- **verify-*** commands are read-only: an AlertEvent against its sealed inputs; a replay document against its ordered
  alerts (byte-identical); a receipt directory against the alerts it refers to. They never send, write or use the
  network.

Every send writes one receipt **before** the guard records the delivered marker. A receipt write failure never hides
a confirmed send: the marker is still recorded and the command exits 5.

Output: a one-line JSON summary of ids, codes and counts on stdout; errors go to stderr as
``{"result": "FAILED", "exit_code": n, "error": "..."}``. No secrets, payloads or rendered text.

Exit codes: 0 success or a valid no-op (``NO_ALERT``, ``duplicate``, ``terminal_suppressed``, ``already_delivered``);
2 usage or invalid input (including an alert that Phase 12C hasn't accepted, for ``deliver``); 3 alert or delivery
state unavailable, conflicting, or busy (another sender holds the delivery lease); 4 delivery failed (the receipt
records it, and the alert stays retryable with ``deliver``); 5 receipt or output storage failure.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import sys
import tempfile

from alert_engine import builder, receipts as rc
from alert_engine.canonical import canonical_json
from alert_engine.state import NEW, replay as replay_alerts, subject_key
from alert_engine.validation import AlertInputError, validated_alert, verify_alert

OK, INVALID, STATE, DELIVERY, STORAGE = 0, 2, 3, 4, 5
REPLAY_FORMAT_VERSION = "phase12-replay-v1"
MANIFEST_FORMAT_VERSION = "phase12-replay-manifest-v1"
DEFAULT_REDIS_URL_ENV = "MIAS_ALERT_REDIS_URL"
DEFAULT_NAMESPACE = "mias:phase12"
TOKEN_ENV, CHAT_ENV = "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
# KEYS[1] delivered marker; ARGV[1] value to write when absent; ARGV[2..] message ids the receipts prove.
# Returns 1 written, 0 already consistent, -1 conflicting.
RESTORE_DELIVERED = """
local current = redis.call('get', KEYS[1])
if current == false then redis.call('set', KEYS[1], ARGV[1]); return 1 end
for i = 2, #ARGV do if current == ARGV[i] then return 0 end end
return -1
"""


class Failure(Exception):
    def __init__(self, code, message, summary=None):
        super().__init__(message)
        self.code, self.summary = code, summary or {}


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def read_json(path, name):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_no_duplicate_keys)
    except FileNotFoundError:
        raise Failure(INVALID, f"{name} file not found") from None
    except (OSError, UnicodeDecodeError, ValueError):
        raise Failure(INVALID, f"{name} file is unreadable, not JSON, or has duplicate keys") from None


def write_atomic(path, text, *, overwrite=False):
    path = os.path.abspath(path)
    if not overwrite and os.path.lexists(path):
        raise Failure(STORAGE, "output file already exists (use --overwrite to replace it)")
    try:
        fd, temp = tempfile.mkstemp(prefix=".alert-runner-", suffix=".tmp", dir=os.path.dirname(path))
    except OSError:
        raise Failure(STORAGE, "cannot create a temporary file in the output directory") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    except FileExistsError:
        raise Failure(STORAGE, "output file already exists (use --overwrite to replace it)") from None
    except OSError:
        raise Failure(STORAGE, "cannot write the output file") from None
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return os.path.getsize(path)


def _alert(value, name="alert"):
    try:
        return validated_alert(value)
    except AlertInputError as error:
        raise Failure(INVALID, f"{name} is invalid: {error}") from None


def ordered_alerts(args):
    """The explicit alert sequence, in the operator's order (never sorted)."""
    if args.manifest:
        manifest = read_json(args.manifest, "manifest")
        if not (isinstance(manifest, dict) and set(manifest) == {"manifest_format_version", "alerts"}
                and manifest["manifest_format_version"] == MANIFEST_FORMAT_VERSION
                and isinstance(manifest["alerts"], list)
                and all(isinstance(p, str) and p for p in manifest["alerts"])):
            raise Failure(INVALID, "manifest is malformed")
        base = os.path.dirname(os.path.abspath(args.manifest))
        paths = [os.path.join(base, p) for p in manifest["alerts"]]
    else:
        paths = args.alert
    return [_alert(read_json(path, f"alert #{i}"), f"alert #{i}") for i, path in enumerate(paths, 1)]


def replay_document(alerts):
    """The deterministic replay document: input order, per-event decisions and the final Phase 12C state."""
    try:
        result = replay_alerts(alerts)
    except AlertInputError as error:
        raise Failure(INVALID, f"replay input is invalid: {error}") from None
    return dict(replay_format_version=REPLAY_FORMAT_VERSION, alert_ids=[a["alert_id"] for a in alerts],
                decisions=result.to_dict()["decisions"], state=result.state.to_dict()), result


def _counts(decisions):
    counts = {}
    for decision in decisions:
        counts[decision.classification] = counts.get(decision.classification, 0) + 1
    return dict(sorted(counts.items()))


def built_alert(args):
    """(rule fired?, alert dict or None) from exactly one source mode, verified by re-derivation."""
    modes = [bool(args.assessment), bool(args.invalidation_check),
             bool(args.previous_market_intelligence or args.current_market_intelligence)]
    if sum(modes) != 1:
        raise Failure(INVALID, "give exactly one source: --assessment, --invalidation-check, or both market "
                               "intelligence files")
    sources = {}
    try:
        if args.assessment:
            sources["assessment"] = read_json(args.assessment, "assessment")
            event = builder.setup_available(sources["assessment"])
        elif args.invalidation_check:
            sources["check"] = read_json(args.invalidation_check, "invalidation check")
            event = builder.setup_invalidated(sources["check"])
        else:
            if not (args.previous_market_intelligence and args.current_market_intelligence):
                raise Failure(INVALID, "market_pattern_changed needs both market intelligence files")
            sources["previous"] = read_json(args.previous_market_intelligence, "previous market intelligence")
            sources["current"] = read_json(args.current_market_intelligence, "current market intelligence")
            event = builder.market_pattern_changed(sources["previous"], sources["current"])
        if event is None:
            return None, sources
        data = event.to_dict()
        verify_alert(data, **sources)
    except AlertInputError as error:
        raise Failure(INVALID, f"input is invalid: {error}") from None
    return data, sources


def alert_summary(data):
    return dict(alert_id=data["alert_id"], alert_code=data["alert_code"], subject_key=subject_key(data),
                as_of=data["as_of"])


# Commands ------------------------------------------------------------------------------------------------------------

def cmd_build(args, ctx):
    data, _ = built_alert(args)
    if data is None:
        return dict(result="NO_ALERT")
    size = write_atomic(args.output, canonical_json(data) + "\n", overwrite=args.overwrite)
    return dict(result="WRITTEN", **alert_summary(data), output_bytes=size)


def cmd_replay(args, ctx):
    document, result = replay_document(ordered_alerts(args))
    summary = dict(result="REPLAYED", alerts=len(document["alert_ids"]), classifications=_counts(result.decisions),
                   seen=len(result.state.seen_alert_ids), subjects=len(result.state.subjects))
    if args.output:
        summary.update(result="WRITTEN", output_bytes=write_atomic(args.output, canonical_json(document) + "\n",
                                                                   overwrite=args.overwrite))
    return summary


def cmd_verify_alert(args, ctx):
    data = _alert(read_json(args.alert, "alert"))
    _, sources = built_alert(args)
    try:
        verify_alert(data, **sources)
    except AlertInputError as error:
        raise Failure(INVALID, f"alert does not verify: {error}") from None
    return dict(result="VERIFIED", **alert_summary(data))


def cmd_verify_replay(args, ctx):
    supplied = read_json(args.replay, "replay")
    document, result = replay_document(ordered_alerts(args))
    if canonical_json(supplied) != canonical_json(document):
        raise Failure(INVALID, "replay document does not match a replay of the given alerts")
    return dict(result="VERIFIED", alerts=len(document["alert_ids"]), classifications=_counts(result.decisions))


def cmd_verify_receipts(args, ctx):
    alerts = {a["alert_id"]: a for a in ordered_alerts(args)}
    try:
        history = rc.read_receipts(args.receipt_dir)
    except rc.ReceiptError as error:
        raise Failure(STORAGE, str(error)) from None
    unknown = sorted({r["alert_id"] for r in history} - set(alerts))
    if unknown:
        raise Failure(INVALID, "a receipt refers to an alert that was not supplied")
    markers = rc.delivered_markers(history)
    attempted = {(r["alert_id"], r["channel"]) for r in history}
    return dict(result="VERIFIED", receipts=len(history), delivered=len(markers),
                undelivered=len(attempted - set(markers)),
                duplicate_sends=sum(1 for ids in markers.values() if len(ids) > 1))


def cmd_restore_state(args, ctx):
    _confirm(args)
    alerts = ordered_alerts(args)
    _, result = replay_document(alerts)
    from alert_engine.state_store import AlertStateUnavailable, RedisAlertStateStore
    store = RedisAlertStateStore(ctx.redis(args), args.namespace)
    try:
        written = store.restore(result.state)
    except AlertStateUnavailable as error:
        raise Failure(STATE, str(error)) from None
    return dict(result="RESTORED", alerts=len(alerts), seen=len(result.state.seen_alert_ids),
                subjects=len(result.state.subjects), written_seen=written["seen"],
                written_subjects=written["subjects"])


def delivered_key(namespace, alert_id, channel):
    """The Phase 12D guard's delivered-marker key (``RedisDeliveryGuard.key(request, "delivered")``)."""
    return f"{namespace}:delivery:{channel}:{alert_id.split(':', 1)[1]}:delivered"


def cmd_restore_delivery(args, ctx):
    _confirm(args)
    try:
        markers = rc.delivered_markers(rc.read_receipts(args.receipt_dir))
    except rc.ReceiptError as error:
        raise Failure(STORAGE, str(error)) from None
    import redis
    client = ctx.redis(args)
    keys = {delivered_key(args.namespace, alert_id, channel): ids for (alert_id, channel), ids in markers.items()}
    written = 0
    try:
        for key, ids in keys.items():                       # check everything first: a conflict writes nothing
            current = client.get(key)
            current = current.decode("utf-8") if isinstance(current, bytes) else current
            if current is not None and current not in ids:
                raise Failure(STATE, "a delivered marker disagrees with the delivery receipts; not restored")
        for key, ids in keys.items():
            result = int(client.eval(RESTORE_DELIVERED, 1, key, ids[-1], *ids))
            if result == -1:
                raise Failure(STATE, "a delivered marker disagrees with the delivery receipts; not restored")
            written += result
    except redis.RedisError as error:
        raise Failure(STATE, f"delivery state restore failed ({type(error).__name__})") from None
    return dict(result="RESTORED", delivered=len(keys), written_markers=written)


def _confirm(args):
    if not args.confirm:
        raise Failure(INVALID, "restore writes Redis state; pass --confirm to proceed")


class RecordingAdapter:
    """Wraps the real adapter: times one send and appends its receipt before the guard records the marker. A
    receipt failure is kept for the caller and never hides the provider result."""

    def __init__(self, inner, directory, clock):
        self.inner, self.directory, self.clock = inner, directory, clock
        self.channel = inner.channel
        self.result = self.receipt = self.receipt_error = None

    def send(self, request):
        attempted_at = _instant(self.clock())
        self.result = self.inner.send(request)
        completed_at = _instant(self.clock())
        try:
            _, self.receipt = rc.append_receipt(self.directory, request, self.result, attempted_at, completed_at)
        except rc.ReceiptError as error:
            self.receipt_error = str(error)
        return self.result


def _instant(value):
    if not (isinstance(value, datetime) and value.utcoffset() is not None):
        raise Failure(STORAGE, "clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _deliver(args, ctx, data, summary):
    """Send an accepted alert through the guard with receipts; returns (exit code, summary)."""
    from alert_engine.delivery.guard import ALREADY_DELIVERED, IN_PROGRESS, DeliveryStateUnavailable, RedisDeliveryGuard
    from alert_engine.rendering import delivery_request
    adapter = RecordingAdapter(ctx.adapter, args.receipt_dir, ctx.clock)
    request = delivery_request(data, args.channel)
    guard = RedisDeliveryGuard(ctx.client, args.namespace)
    try:
        outcome = guard.deliver(request, adapter)
    except DeliveryStateUnavailable as error:
        summary.update(_send_fields(adapter))
        raise Failure(STATE, str(error) + _receipt_note(adapter), summary) from None
    except ValueError as error:                              # the adapter refused the request; nothing was sent
        raise Failure(INVALID, f"delivery request refused: {error}", summary) from None
    summary.update(delivery=outcome.action, **_send_fields(adapter))
    if outcome.action == ALREADY_DELIVERED:
        return OK, summary
    if outcome.action == IN_PROGRESS:
        raise Failure(STATE, "another sender holds the delivery lease; retry later with deliver", summary)
    if adapter.receipt_error:
        raise Failure(STORAGE, f"{adapter.receipt_error} (delivery status {outcome.result.status})", summary)
    if outcome.result.status != "delivered":
        return DELIVERY, summary
    return OK, summary


def _send_fields(adapter):
    if adapter.result is None:
        return {}
    fields = dict(status=adapter.result.status, attempts=adapter.result.attempts)
    if adapter.result.error_code:
        fields["safe_error_code"] = adapter.result.error_code
    if adapter.receipt:
        fields["receipt_sequence"] = adapter.receipt["sequence"]
    return fields


def _receipt_note(adapter):
    if adapter.result is None:
        return ""
    if adapter.receipt_error:
        return f"; delivery status {adapter.result.status}, and {adapter.receipt_error}"
    return f"; delivery status {adapter.result.status} is recorded in receipt {adapter.receipt['sequence']}"


def _prepare_delivery(args, ctx):
    """Everything a send needs, checked before any state is touched: the alert, the receipt directory and the
    adapter credentials."""
    data = _alert(read_json(args.alert, "alert"))
    directory = args.receipt_dir
    if not (os.path.isdir(directory) and os.access(directory, os.W_OK | os.X_OK)):
        raise Failure(STORAGE, "receipt directory does not exist or is not writable")
    try:
        rc.read_receipts(directory)
    except rc.ReceiptError as error:
        raise Failure(STORAGE, str(error)) from None
    ctx.adapter = ctx.make_adapter(args.channel)
    ctx.client = ctx.redis(args)
    return data


def cmd_run_once(args, ctx):
    data = _prepare_delivery(args, ctx)
    from alert_engine.state_store import AlertStateUnavailable, RedisAlertStateStore
    try:
        decision = RedisAlertStateStore(ctx.client, args.namespace).record(data)
    except AlertStateUnavailable as error:
        raise Failure(STATE, str(error)) from None
    summary = dict(result="PROCESSED", **alert_summary(data), classification=decision.classification)
    if decision.classification != NEW:
        return OK, summary                                   # duplicate / terminal_suppressed: no render, no send
    return _deliver(args, ctx, data, summary)


def cmd_deliver(args, ctx):
    data = _prepare_delivery(args, ctx)
    import redis
    from alert_engine.state_store import RedisAlertStateStore
    try:
        accepted = bool(ctx.client.exists(RedisAlertStateStore(ctx.client, args.namespace).seen_key(data["alert_id"])))
    except redis.RedisError as error:
        raise Failure(STATE, f"alert state unavailable ({type(error).__name__})") from None
    if not accepted:
        raise Failure(INVALID, "alert has not been accepted by Phase 12C (run run-once or restore-state first)")
    return _deliver(args, ctx, data, dict(result="PROCESSED", **alert_summary(data), classification="accepted"))


# Wiring --------------------------------------------------------------------------------------------------------------

class Context:
    """Runtime dependencies, injectable for tests. Defaults read only the process environment (never ``.env``)."""

    def __init__(self, environ=None, clock=None, redis_factory=None, adapter_factory=None):
        self.environ = os.environ if environ is None else environ
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.redis_factory, self.adapter_factory = redis_factory, adapter_factory
        self.adapter = self.client = None

    def redis(self, args):
        url = self.environ.get(args.redis_url_env)
        if not url:
            raise Failure(INVALID, f"{args.redis_url_env} is not set")
        if self.redis_factory:
            return self.redis_factory(url)
        import redis
        try:
            return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        except ValueError:
            raise Failure(INVALID, f"{args.redis_url_env} is not a valid Redis URL") from None

    def make_adapter(self, channel):
        token, chat = self.environ.get(TOKEN_ENV), self.environ.get(CHAT_ENV)
        if not token or not chat:
            raise Failure(INVALID, f"{TOKEN_ENV} and {CHAT_ENV} must be set in the process environment")
        if self.adapter_factory:
            return self.adapter_factory(channel, token, chat)
        from alert_engine.delivery.telegram import TelegramAdapter
        return TelegramAdapter(token, chat)


def _parser():
    parser = argparse.ArgumentParser(prog="python -m alert_engine.runner")
    commands = parser.add_subparsers(dest="command", required=True)

    def sources(p):
        p.add_argument("--assessment")
        p.add_argument("--invalidation-check")
        p.add_argument("--previous-market-intelligence")
        p.add_argument("--current-market-intelligence")

    def sequence(p):
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--alert", action="append")
        group.add_argument("--manifest")

    def redis_options(p):
        p.add_argument("--redis-url-env", default=DEFAULT_REDIS_URL_ENV)
        p.add_argument("--namespace", default=DEFAULT_NAMESPACE)

    def delivery(p):
        p.add_argument("--alert", required=True)
        p.add_argument("--receipt-dir", required=True)
        p.add_argument("--channel", default="telegram", choices=("telegram",))
        redis_options(p)

    p = commands.add_parser("build")
    sources(p)
    p.add_argument("--output", required=True)
    p.add_argument("--overwrite", action="store_true")
    p = commands.add_parser("replay")
    sequence(p)
    p.add_argument("--output")
    p.add_argument("--overwrite", action="store_true")
    p = commands.add_parser("restore-state")
    sequence(p)
    p.add_argument("--confirm", action="store_true")
    redis_options(p)
    p = commands.add_parser("restore-delivery")
    p.add_argument("--receipt-dir", required=True)
    p.add_argument("--confirm", action="store_true")
    redis_options(p)
    delivery(commands.add_parser("run-once"))
    delivery(commands.add_parser("deliver"))
    p = commands.add_parser("verify-alert")
    p.add_argument("--alert", required=True)
    sources(p)
    p = commands.add_parser("verify-replay")
    p.add_argument("--replay", required=True)
    sequence(p)
    p = commands.add_parser("verify-receipts")
    p.add_argument("--receipt-dir", required=True)
    sequence(p)
    return parser


COMMANDS = {"build": cmd_build, "replay": cmd_replay, "restore-state": cmd_restore_state,
            "restore-delivery": cmd_restore_delivery, "run-once": cmd_run_once, "deliver": cmd_deliver,
            "verify-alert": cmd_verify_alert, "verify-replay": cmd_verify_replay,
            "verify-receipts": cmd_verify_receipts}


def main(argv=None, *, out=None, err=None, environ=None, clock=None, redis_factory=None, adapter_factory=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return INVALID if exit_.code else OK
    if getattr(args, "overwrite", False) and not getattr(args, "output", None):
        print(json.dumps(dict(result="FAILED", exit_code=INVALID, error="--overwrite needs --output")), file=err)
        return INVALID
    ctx = Context(environ, clock, redis_factory, adapter_factory)
    try:
        returned = COMMANDS[args.command](args, ctx)
        code, summary = returned if isinstance(returned, tuple) else (OK, returned)
    except Failure as failure:
        print(json.dumps(dict(failure.summary, command=args.command, result="FAILED", exit_code=failure.code,
                              error=str(failure)), sort_keys=True), file=err)
        return failure.code
    print(json.dumps(dict(command=args.command, exit_code=code, **summary), sort_keys=True), file=out)
    return code


if __name__ == "__main__":
    sys.exit(main())
