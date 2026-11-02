#!/usr/bin/env python3
"""Project-state helper for OpenCode sessions: PROJECT_STATE.md, secret lint, git checkpoints, resume prompt.

  ocstate.py init [dir]              create PROJECT_STATE.md + AGENTS.md (never overwrites)
  ocstate.py lint [dir]              fail if PROJECT_STATE.md contains something that looks like a secret
  ocstate.py checkpoint "message"    snapshot the working tree to refs/gateway-checkpoints/* (does NOT touch your branch/index)
  ocstate.py checkpoints             list checkpoints
  ocstate.py diff [ref]              what changed since a checkpoint (default: latest)
  ocstate.py restore REF --yes       restore files from a checkpoint (takes a safety checkpoint first)
  ocstate.py resume                  print a prompt for task-level recovery after an interruption
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "..", "templates")
REF_PREFIX = "refs/gateway-checkpoints/"
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), re.compile(r"gsk_[A-Za-z0-9]{12,}"), re.compile(r"nvapi-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{12,}"),
]


def git(*args, cwd=None, env=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def root(cwd="."):
    return git("rev-parse", "--show-toplevel", cwd=cwd)


def cmd_init(a):
    d = os.path.abspath(a.dir)
    for name in ("PROJECT_STATE.md", "AGENTS.md"):
        dst = os.path.join(d, name)
        if os.path.exists(dst):
            print("exists, left untouched:", name)
        else:
            shutil.copy(os.path.join(TEMPLATES, name), dst)
            print("created:", name)
    if not os.path.isdir(os.path.join(d, ".git")):
        print("NOTE: %s is not a git repository; checkpoints need one (run: git init)" % d)
    return 0


def lint_text(text):
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for p in SECRET_PATTERNS):
            hits.append(i)
    return hits


def cmd_lint(a):
    path = os.path.join(a.dir, "PROJECT_STATE.md")
    if not os.path.exists(path):
        print("PROJECT_STATE.md not found"); return 2
    hits = lint_text(open(path, encoding="utf-8", errors="replace").read())
    if hits:
        print("POSSIBLE SECRET in PROJECT_STATE.md on line(s): %s - remove it" % ", ".join(map(str, hits)))
        return 1
    print("PROJECT_STATE.md: no secret-like strings found (heuristic check)")
    return 0


def cmd_checkpoint(a):
    top = root()
    index = git("rev-parse", "--git-path", "index", cwd=top)
    index = index if os.path.isabs(index) else os.path.join(top, index)
    tmpdir = tempfile.mkdtemp()
    tmp_index = os.path.join(tmpdir, "index")
    if os.path.exists(index):
        shutil.copy(index, tmp_index)
    env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
    if not git("config", "user.name", cwd=top, check=False):
        env.update(GIT_AUTHOR_NAME="oc-checkpoint", GIT_AUTHOR_EMAIL="checkpoint@localhost",
                   GIT_COMMITTER_NAME="oc-checkpoint", GIT_COMMITTER_EMAIL="checkpoint@localhost")
    git("add", "-A", cwd=top, env=env)                           # temporary index: the user's real index is untouched
    tree = git("write-tree", cwd=top, env=env)
    shutil.rmtree(tmpdir, ignore_errors=True)
    last = git("for-each-ref", "--sort=-creatordate", "--count=1", "--format=%(refname)", REF_PREFIX, cwd=top)
    if last and git("rev-parse", last + "^{tree}", cwd=top) == tree:
        print("no changes since last checkpoint:", last.replace(REF_PREFIX, "")); return 0
    head = git("rev-parse", "-q", "--verify", "HEAD", cwd=top, check=False)
    args = ["commit-tree", tree, "-m", a.message or "checkpoint"] + (["-p", head] if head else [])
    commit = git(*args, cwd=top, env=env)
    name = time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while git("rev-parse", "-q", "--verify", REF_PREFIX + name, cwd=top, check=False):   # never overwrite an existing checkpoint
        n += 1
        name = "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), n)
    git("update-ref", REF_PREFIX + name, commit, cwd=top)
    print("checkpoint created:", name, commit[:10])
    return 0


def cmd_checkpoints(a):
    out = git("for-each-ref", "--sort=-creatordate", "--format=%(refname:short)|%(subject)", REF_PREFIX, cwd=root())
    print(out.replace("gateway-checkpoints/", "") or "no checkpoints")
    return 0


def _latest(top):
    return git("for-each-ref", "--sort=-creatordate", "--count=1", "--format=%(refname)", REF_PREFIX, cwd=top)


def cmd_diff(a):
    top = root()
    ref = (REF_PREFIX + a.ref) if a.ref else _latest(top)
    if not ref:
        print("no checkpoints"); return 1
    print(git("diff", "--stat", ref, cwd=top) or "working tree identical to checkpoint")
    return 0


def cmd_restore(a):
    top = root()
    if not a.yes:
        print("Restoring overwrites files in the working tree. Re-run with --yes to confirm (a safety checkpoint is taken first).")
        return 1
    source = git("rev-parse", "--verify", REF_PREFIX + a.ref, cwd=top)     # pin the commit before anything else happens
    a.message = "pre-restore safety checkpoint"
    cmd_checkpoint(a)
    git("restore", "--source=" + source, "--worktree", "--", ".", cwd=top)
    print("restored files from", a.ref, "(files created after the checkpoint are NOT deleted)")
    return 0


def cmd_resume(a):
    top = root()
    state_path = os.path.join(top, "PROJECT_STATE.md")
    state = open(state_path, encoding="utf-8").read() if os.path.exists(state_path) else "(PROJECT_STATE.md missing)"
    print("""The previous request was interrupted (provider switch, timeout or crash). Resume at TASK level, not token level.
Rules: do not assume the interrupted step completed; verify the repository first; do not trust unverified claims.

1. Run `git status` and `git diff --stat`; compare with the "Modified files" and "Current task" below.
2. Re-run the tests mentioned under "Test results" if they relate to the current task.
3. Continue from "Next actions" and update PROJECT_STATE.md as you confirm things.

----- git status -----
%s
----- last checkpoint -----
%s
----- PROJECT_STATE.md -----
%s""" % (git("status", "--short", cwd=top) or "(clean)", (_latest(top) or "none").replace(REF_PREFIX, ""), state))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    for n, f in (("init", cmd_init), ("lint", cmd_lint)):
        s = sp.add_parser(n); s.add_argument("dir", nargs="?", default="."); s.set_defaults(f=f)
    s = sp.add_parser("checkpoint"); s.add_argument("message", nargs="?", default="checkpoint"); s.set_defaults(f=cmd_checkpoint)
    s = sp.add_parser("checkpoints"); s.set_defaults(f=cmd_checkpoints)
    s = sp.add_parser("diff"); s.add_argument("ref", nargs="?"); s.set_defaults(f=cmd_diff)
    s = sp.add_parser("restore"); s.add_argument("ref"); s.add_argument("--yes", action="store_true"); s.set_defaults(f=cmd_restore)
    s = sp.add_parser("resume"); s.set_defaults(f=cmd_resume)
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
