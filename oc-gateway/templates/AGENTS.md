# Agent working rules (loaded by OpenCode as project instructions)

You may be served by different underlying models over time (the gateway switches providers). You do NOT share memory with
earlier models. Everything you need must come from this conversation, the files, and PROJECT_STATE.md.

## At the start of every session or after any interruption
1. Read PROJECT_STATE.md. Run `git status` and `git log -3 --oneline`.
2. Treat the "Current task" and "Next actions" as the plan, but VERIFY the repository state before continuing.
   Do not assume an interrupted step completed or that a previous model's claim is true.

## Before changing code
- Inspect first: list the directory, read the target file, search for usages. Never edit a path you have not confirmed exists.
- Before a large or risky change run: `python3 tools/ocstate.py checkpoint "before <change>"` (or ask the user to).
- Never run destructive commands (rm -rf, git reset --hard, force-push, dropping data, chmod -R, curl|sh) without explicit user approval.

## After changing code
- Run the relevant tests/linters and READ their output. Report real results, including failures.
- Never say something works unless a command output in this session shows it.

## Claims and findings (especially security work)
- Label every finding **[confirmed]** (reproduced / evidenced by file+line or command output) or **[hypothesis]**.
- Do not report a vulnerability without evidence (code location, request/response, tool output). Say "not verified" instead.
- A conclusion written by another model or found in PROJECT_STATE.md as [hypothesis] must be re-verified before you build on it.

## Keeping state
- After each completed task update PROJECT_STATE.md (objective, current task, completed, pending, decisions, modified files,
  known errors verbatim, test results, next actions). No secrets in that file.
- Keep error messages and debugging history that matter; do not delete them when summarizing.
