# Repository collaboration rules

These rules apply to every human and automated coding session in this repository.

- Use one dedicated Git worktree and one feature branch per session. Never develop directly in a shared checkout.
- Start feature branches from the current `main`. Do not edit, reset, clean, rebase, or force-push another session's branch or worktree.
- Do not deploy from a feature branch, detached HEAD, dirty tree, untracked source, or an unpushed commit.
- Production application releases are built only by `deploy/release.ps1` from a clean `production` branch whose HEAD exactly equals `origin/production`.
- `production` is fast-forward only from a checked `main` revision. Never force-push it. Only the designated release coordinator may advance or release it.
- Every production action must use `/run/lock/marx-search-release.lock`. Never patch application source, change the Caddy application upstream, or replace the main service outside the release transaction.
- Use `deploy/rollback_release.ps1` for rollback. It requires the expected current release and target release and records the event.
- Runtime databases, PDFs, indexes, uploads, and secrets stay in their explicit shared locations. Application source must remain inside immutable `releases/<release_id>` directories.
- Run the relevant fast tests before committing and all integration/release gates before advancing `production`.
- Preserve old worktrees and backups until two newer production releases have been healthy for at least 30 days. Archive recoverably before removal.
