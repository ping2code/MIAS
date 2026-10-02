"""Deterministic rendering of a sealed AlertEvent (``phase12-render-v1``). Pure.

``render(alert)`` produces fixed-order plain text from the validated AlertEvent alone: its code, subject, sealed facts,
transition, ``as_of`` and a short alert reference. There is no clock, environment, configuration, network, Redis or
AI, and no URLs, payloads, secrets or free text. Only closed labels and the alert's own values appear: no candidate
contract data, strike, expiration, ranking, recommendation, prediction, severity or profit/loss language.

``delivery_request(alert, channel)`` wraps the rendered text in a provider-neutral ``DeliveryRequest``.
"""
from alert_engine import rules as r
from alert_engine.delivery.base import DeliveryRequest
from alert_engine.validation import validated_alert

RENDER_VERSION = "phase12-render-v1"
HEADER = "MIAS phase12 alert"
EVENT_NAMES = {r.SETUP_AVAILABLE: "setup available", r.SETUP_INVALIDATED: "setup invalidated",
               r.MARKET_PATTERN_CHANGED: "market pattern changed"}


def short_id(content_id):
    """``sha256:`` plus the first 16 hex digits: a stable reference, not an identity."""
    return content_id[:23]


def _lines(data):
    facts, subject = data["facts"], data["subject"]
    code = data["alert_code"]
    lines = [f"{HEADER} ({RENDER_VERSION})", f"Event: {EVENT_NAMES[code]}", f"Symbol: {subject['symbol']}"]
    if code == r.SETUP_AVAILABLE:
        lines += [f"Market bias: {facts['market_bias']}", f"Eligible side: {facts['eligible_side']}",
                  f"Candidate count: {facts['candidate_count']}", f"Setup: {short_id(subject['assessment_id'])}"]
    elif code == r.SETUP_INVALIDATED:
        lines += [f"Side: {facts['side']}", f"Required pattern: {facts['required_pattern']}",
                  f"Observed pattern: {facts['observed_pattern']}",
                  f"Observed technical status: {facts['observed_technical_status']}",
                  f"Setup: {short_id(subject['assessment_id'])}"]
    else:
        lines += [f"Previous pattern: {data['transition']['previous']}",
                  f"Current pattern: {data['transition']['current']}", f"Elapsed seconds: {facts['elapsed_seconds']}"]
    return lines + [f"As of: {data['as_of']}", f"Alert: {short_id(data['alert_id'])}"]


def render(alert):
    """Deterministic plain text for a sealed AlertEvent (validated first; fails closed)."""
    return "\n".join(_lines(validated_alert(alert)))


def delivery_request(alert, channel):
    data = validated_alert(alert)
    return DeliveryRequest(data["alert_id"], channel, RENDER_VERSION, "\n".join(_lines(data)))
