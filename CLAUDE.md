# Polytone — working rules

## Git workflow (every feature change)
`main` is never committed to directly. For each feature/fix:
1. `git switch -c <type>/<short-name>` from an up-to-date `main` (`git pull --ff-only`).
2. Commit the work (run the relevant tests first; don't open a PR with failing tests).
3. `git push -u origin HEAD`
4. `gh pr create --fill` (or with a written title/body).
5. `gh pr checks --watch --fail-fast` — wait for GitHub Actions CI (`.github/workflows/ci.yml`:
   backend pytest + ruff, frontend build, and macOS cargo test + clippy when `src-tauri/` changed).
   If the **CI passed** check fails, fix it on the branch and push again. Never merge a red PR.
6. `gh pr merge --squash --delete-branch` once CI is green.
7. `git switch main && git pull --ff-only`.

Repo: https://github.com/cubeerea/polytone (private). Never commit `.env`, `credentials.json`, `token.json`, or anything under `data/`.

## Unattended agent loop
`orchestrator.py` runs headless Claude Code on one task from `progress.json` (git-ignored) as an
implementor/evaluator pair: the implementor makes the change, `validate.py` (the same checks as CI)
must pass, then a read-only evaluator agent reviews the diff against the task. Validation failures and
evaluator rejections are fed back until it passes or hits `max_iterations`. With
`run --watch --pr --routine` it also queues routine work when idle (validation sweep → tests → bug hunt,
every 6h by default; none while 2 routine PRs are open). Queue tasks in the worktree the loop runs in:
each checkout has its own `progress.json`. Run it in its own worktree (`git worktree add ../polytone-agent main`) inside tmux.
The agent commits on `agent/<task>` only; pushing, PRs and merging stay with the orchestrator/human.
Agents must never edit `validate.py`, `orchestrator.py`, `progress.json` or `.github/`.
