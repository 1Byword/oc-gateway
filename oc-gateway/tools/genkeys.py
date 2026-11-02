#!/usr/bin/env python3
"""Generate the gateway's own secrets. Prints values for .env (server) and the client key (for OpenCode)."""
import hashlib
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
from app.secrets_box import SecretBox  # noqa: E402

gw = "gw-" + secrets.token_urlsafe(32)
adm = "adm-" + secrets.token_urlsafe(32)
h = lambda s: hashlib.sha256(s.encode()).hexdigest()
print("# ---- put these in the SERVER .env (only hashes + the master key; never commit this file) ----")
print("GATEWAY_MASTER_KEY=" + SecretBox.generate())
print("GATEWAY_KEY_HASHES=" + h(gw))
print("ADMIN_TOKEN_HASHES=" + h(adm))
print()
print("# ---- keep these two PRIVATE on your side; they are shown only once ----")
print("CLIENT KEY (OpenCode -> export GATEWAY_KEY=...): " + gw)
print("ADMIN TOKEN (dashboard login):                   " + adm)
