"""Validation of model output before it is handed to the client (non-streaming responses)."""
import json


def validate_tool_calls(resp):
    """Return None if fine, else a string describing why the tool calls are unusable."""
    try:
        for ch in resp.get("choices", []):
            for tc in (ch.get("message") or {}).get("tool_calls") or []:
                fn = tc.get("function") or {}
                if not fn.get("name"):
                    return "tool call without function name"
                args = fn.get("arguments", "")
                if isinstance(args, str) and args.strip():
                    json.loads(args)
    except (ValueError, AttributeError, TypeError) as e:
        return "invalid tool call arguments: %s" % type(e).__name__
    return None


def validate_completion(resp):
    if not isinstance(resp, dict) or not isinstance(resp.get("choices"), list) or not resp["choices"]:
        return "response has no choices"
    return validate_tool_calls(resp)
