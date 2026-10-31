# Polytone — working rules

## Git workflow (every feature change)
`main` is never committed to directly. For each feature/fix:
1. `git switch -c <type>/<short-name>` from an up-to-date `main` (`git pull --ff-only`).
2. Commit the work (run the relevant tests first; don't open a PR with failing tests).
3. `git push -u origin HEAD`
4. `gh pr create --fill` (or with a written title/body).
5. `gh pr merge --squash --delete-branch` — merges right away (no required checks are configured).
6. `git switch main && git pull --ff-only`.

Repo: https://github.com/cubeerea/polytone (private). Never commit `.env`, `credentials.json`, `token.json`, or anything under `data/`.
