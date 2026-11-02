import os
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ocstate.py")


def run(cwd, *a):
    return subprocess.run([sys.executable, TOOL, *a], cwd=cwd, capture_output=True, text=True)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class TestOcState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        git(self.d, "init", "-q")
        git(self.d, "config", "user.email", "t@t"); git(self.d, "config", "user.name", "t")
        open(os.path.join(self.d, "a.py"), "w").write("print(1)\n")
        git(self.d, "add", "-A"); git(self.d, "commit", "-qm", "init")

    def test_init_creates_files_and_never_overwrites(self):
        self.assertEqual(run(self.d, "init").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "PROJECT_STATE.md")))
        open(os.path.join(self.d, "PROJECT_STATE.md"), "w").write("mine")
        run(self.d, "init")
        self.assertEqual(open(os.path.join(self.d, "PROJECT_STATE.md")).read(), "mine")

    def test_lint_detects_secrets_and_passes_clean_file(self):
        run(self.d, "init")
        self.assertEqual(run(self.d, "lint").returncode, 0)
        with open(os.path.join(self.d, "PROJECT_STATE.md"), "a") as f:
            f.write("\n- uses key gsk_abcdefghijklmnop1234\n")
        r = run(self.d, "lint")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("gsk_abcdefghijklmnop1234", r.stdout)       # the tool itself must not echo the secret

    def test_checkpoint_does_not_touch_branch_or_index_and_includes_untracked(self):
        head = git(self.d, "rev-parse", "HEAD")
        open(os.path.join(self.d, "a.py"), "w").write("print(2)\n")
        open(os.path.join(self.d, "new.py"), "w").write("x=1\n")
        before = git(self.d, "status", "--porcelain")
        r = run(self.d, "checkpoint", "before refactor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git(self.d, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.d, "status", "--porcelain"), before)           # index/worktree untouched
        ref = git(self.d, "for-each-ref", "--format=%(refname)", "refs/gateway-checkpoints/")
        self.assertTrue(ref)
        self.assertIn("new.py", git(self.d, "ls-tree", "-r", "--name-only", ref))  # untracked file captured
        self.assertIn("no changes", run(self.d, "checkpoint", "again").stdout)

    def test_restore_requires_confirmation_then_restores(self):
        open(os.path.join(self.d, "a.py"), "w").write("good\n")
        run(self.d, "checkpoint", "good state")
        name = run(self.d, "checkpoints").stdout.split("|")[0].strip()
        open(os.path.join(self.d, "a.py"), "w").write("broken\n")
        self.assertEqual(run(self.d, "restore", name).returncode, 1)
        self.assertEqual(open(os.path.join(self.d, "a.py")).read(), "broken\n")   # nothing happened without --yes
        self.assertEqual(run(self.d, "restore", name, "--yes").returncode, 0)
        self.assertEqual(open(os.path.join(self.d, "a.py")).read(), "good\n")
        self.assertGreaterEqual(len(run(self.d, "checkpoints").stdout.strip().splitlines()), 2)   # safety checkpoint taken

    def test_resume_prompt_contains_state_and_warning(self):
        run(self.d, "init")
        out = run(self.d, "resume").stdout
        self.assertIn("TASK level", out)
        self.assertIn("## Next actions", out)
        self.assertIn("do not assume the interrupted step completed", out)


if __name__ == "__main__":
    unittest.main()
