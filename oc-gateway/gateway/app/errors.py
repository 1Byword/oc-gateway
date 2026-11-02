"""Upstream error classification. Decides what is retryable and how long to cool down."""
import json
import re
import requests

from .util import parse_retry_after, redact

QUOTA_HINTS = re.compile(r"(insufficient_quota|quota|per[- ]day|daily|exceeded your current|credits?|billing|free-models-per-day|out of (tokens|requests))", re.I)
CONTEXT_HINTS = re.compile(r"(context[_ ]length|maximum context|context window|too many tokens|prompt is too long|reduce the length)", re.I)


class UpstreamError(Exception):
    def __init__(self, kind, status=None, detail="", retry_after=None, retryable=True):
        super().__init__("%s %s %s" % (kind, status, detail))
        self.kind, self.status, self.detail = kind, status, redact(detail)[:400]
        self.retry_after, self.retryable = retry_after, retryable


def classify_response(status, body_text, headers, now):
    text = body_text or ""
    ra = parse_retry_after((headers or {}).get("Retry-After") or (headers or {}).get("retry-after"), now)
    if status == 429:
        if QUOTA_HINTS.search(text) or (ra is not None and ra > 900):
            return UpstreamError("quota", status, text, ra)
        return UpstreamError("rate_limit", status, text, ra)
    if status == 402:
        return UpstreamError("quota", status, text, ra)
    if status in (401, 403):
        return UpstreamError("auth", status, text, None)
    if status == 404:
        return UpstreamError("model_unavailable", status, text, None)
    if status in (400, 413, 422):
        if CONTEXT_HINTS.search(text):
            return UpstreamError("context_length", status, text, None)
        return UpstreamError("bad_request", status, text, None, retryable=False)
    if status == 408:
        return UpstreamError("timeout", status, text, ra)
    if status >= 500:
        return UpstreamError("server", status, text, ra)
    return UpstreamError("bad_request", status, text, None, retryable=False)


def classify_exception(exc):
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return UpstreamError("connection", None, "connect timeout")
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return UpstreamError("timeout", None, "read timeout")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return UpstreamError("connection", None, "connection error: %s" % type(exc).__name__)
    if isinstance(exc, (requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError)):
        return UpstreamError("connection", None, "stream interrupted")
    return UpstreamError("server", None, "%s" % type(exc).__name__)


def error_from_stream_event(obj):
    """Some providers return HTTP 200 and put an error object in the first SSE event."""
    err = obj.get("error")
    if not err:
        return None
    msg = json.dumps(err)[:300]
    code = str(err.get("code", "")) if isinstance(err, dict) else ""
    if code == "429" or "rate" in msg.lower():
        return UpstreamError("quota" if QUOTA_HINTS.search(msg) else "rate_limit", 200, msg)
    if code in ("401", "403"):
        return UpstreamError("auth", 200, msg)
    return UpstreamError("server", 200, msg)
