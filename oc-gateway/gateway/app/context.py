"""Context-window management. Never silently drops information."""
import json
import logging

from .util import est_tokens

log = logging.getLogger("gateway.context")


def estimate_prompt_tokens(payload):
    """Conservative estimate (chars/3.5). It is an estimate, never presented as exact."""
    chars = len(json.dumps(payload.get("messages", []), ensure_ascii=False))
    chars += len(json.dumps(payload.get("tools", []), ensure_ascii=False)) if payload.get("tools") else 0
    return est_tokens(chars)


def fits(payload, model_cfg, margin):
    ctx = model_cfg.get("context", 0)
    if not ctx:
        return True          # unknown window: cannot judge, let the provider decide (it will answer context_length)
    need = estimate_prompt_tokens(payload) + int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
    return need <= ctx * margin


SUMMARY_PROMPT = (
    "You compress an AI-coding-assistant conversation. Produce a faithful, detailed summary of the OLDER part of the "
    "conversation below. You MUST keep verbatim: file paths, function/class names, commands run and their key output, "
    "error messages, test results, decisions made and why, TODOs, constraints given by the user. Do not invent facts. "
    "Mark anything uncertain as 'unverified'. Use compact bullet points."
)


def split_for_compaction(messages, keep_last):
    """Return (head_system, middle, tail). The tail never starts with an orphan tool result."""
    system = [m for m in messages[:1] if m.get("role") == "system"] if messages else []
    rest = messages[len(system):]
    if len(rest) <= keep_last:
        return system, [], rest
    cut = len(rest) - keep_last
    while cut > 0 and rest[cut].get("role") == "tool":     # keep assistant tool_calls together with their results
        cut -= 1
    return system, rest[:cut], rest[cut:]


def render_for_summary(middle, limit_chars=60000):
    out = []
    for m in middle:
        c = m.get("content")
        if not isinstance(c, str):
            c = json.dumps(c, ensure_ascii=False)
        if m.get("tool_calls"):
            c += " [tool_calls: %s]" % json.dumps(m["tool_calls"], ensure_ascii=False)
        out.append("%s: %s" % (m.get("role"), c))
    text = "\n".join(out)
    if len(text) <= limit_chars:
        return text, False
    return text[:limit_chars // 2] + "\n...[middle truncated for summarizer input]...\n" + text[-limit_chars // 2:], True
