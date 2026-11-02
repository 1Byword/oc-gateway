import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))
import requests                                  # noqa: E402
from app.server import create_app                # noqa: E402
from app.secrets_box import SecretBox            # noqa: E402
import yaml                                      # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestShippedArtifacts(unittest.TestCase):
    def test_shipped_providers_yaml_boots_the_app(self):
        env = {"GATEWAY_MASTER_KEY": SecretBox.generate(), "GATEWAY_KEY_HASHES": "x", "ADMIN_TOKEN_HASHES": "y", "GROQ_API_KEY": "gsk_testtesttest1234"}
        app = create_app(os.path.join(ROOT, "config", "providers.yaml"), os.path.join(tempfile.mkdtemp(), "t.sqlite"), env)
        c = app.test_client()
        self.assertEqual(c.get("/health").json["status"], "ok")
        keys = app.gateway.store.q("SELECT provider,label,last4 FROM keys")
        self.assertEqual(keys, [{"provider": "groq", "label": "env:GROQ_API_KEY", "last4": "1234"}])     # env key imported, encrypted
        self.assertEqual(c.get("/v1/models").status_code, 401)

    def test_dashboard_served_with_strict_csp_and_no_secrets(self):
        env = {"GATEWAY_MASTER_KEY": SecretBox.generate()}
        app = create_app(os.path.join(ROOT, "config", "providers.yaml"), os.path.join(tempfile.mkdtemp(), "t.sqlite"), env)
        r = app.test_client().get("/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(app.test_client().get("/admin/api/status").status_code, 401)

    def test_dashboard_js_never_uses_innerhtml(self):
        js = open(os.path.join(ROOT, "gateway", "static", "app.js")).read()
        for bad in ("innerHTML", "outerHTML", "document.write", "eval("):
            self.assertNotIn(bad, js)

    def test_dashboard_js_syntax(self):
        if subprocess.run(["which", "node"], capture_output=True).returncode != 0:
            self.skipTest("node not installed")
        self.assertEqual(subprocess.run(["node", "--check", os.path.join(ROOT, "gateway", "static", "app.js")], capture_output=True).returncode, 0)

    def test_opencode_config_is_valid_and_points_at_aliases(self):
        cfg = json.load(open(os.path.join(ROOT, "opencode.json")))
        aliases = yaml.safe_load(open(os.path.join(ROOT, "config", "providers.yaml")))["aliases"]
        self.assertTrue(cfg["provider"]["gateway"]["options"]["baseURL"].endswith("/v1"))
        self.assertEqual(cfg["provider"]["gateway"]["options"]["apiKey"], "{env:GATEWAY_KEY}")      # no literal secret
        for name in cfg["provider"]["gateway"]["models"]:
            self.assertIn(name, aliases)
        self.assertEqual(cfg["model"].split("/")[1] in aliases, True)

    def test_no_secret_like_strings_in_repo_files(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ocstate
        for base, _, files in os.walk(ROOT):
            if any(x in base for x in ("__pycache__", ".git", "tests")):
                continue
            for f in files:
                if f.endswith((".py", ".yaml", ".yml", ".json", ".md", ".js", ".html", ".example", ".sh", "Caddyfile")):
                    txt = open(os.path.join(base, f), encoding="utf-8", errors="replace").read()
                    hits = [l for l in txt.splitlines() if ("sk-" in l or "gsk_" in l or "nvapi-" in l) and any(p.search(l) for p in ocstate.SECRET_PATTERNS[:3])]
                    self.assertEqual(hits, [], f)


if __name__ == "__main__":
    unittest.main()
