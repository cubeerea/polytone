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
