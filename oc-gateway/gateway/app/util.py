import hashlib
import re
import email.utils
import logging

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|gsk_[A-Za-z0-9]{8,}|nvapi-[A-Za-z0-9_\-]{8,}|"
    r"AIza[0-9A-Za-z_\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{8,})"
)


def redact(text):
    return SECRET_RE.sub("[REDACTED]", str(text))


class RedactFilter(logging.Filter):
    """Last line of defence: scrub anything that looks like an API key from log lines."""

    def filter(self, record):
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def mask_key(key):
    return "****" + key[-4:] if len(key) >= 8 else "****"


def sha256_hex(s):
    return hashlib.sha256(s.encode()).hexdigest()


_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")


def parse_duration(value):
    """'1m30s' / '250ms' / '59.5s' / '12' -> seconds (float) or None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    try:
        return float(s)
    except ValueError:
        pass
    parts = _DUR.findall(s)
    if not parts:
        return None
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(float(n) * mult[u] for n, u in parts)


def parse_retry_after(value, now):
    if value is None:
        return None
    d = parse_duration(value)
    if d is not None:
        return d
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        return max(0.0, dt.timestamp() - now)
    except Exception:
        return None


def est_tokens(chars):
    return int(chars / 3.5) + 1
