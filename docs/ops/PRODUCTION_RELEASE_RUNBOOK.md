# Production release runbook

## Current state

Normal production publication is frozen until the baseline and release transaction have passed the release-gate rehearsal. Development may continue in isolated feature worktrees. An emergency production change has one release coordinator and uses the same transaction described below.

The authoritative source baseline is the annotated tag `production-baseline-20260921-140619Z`. The read-only capture and the original dirty-worktree archive are stored outside the repository. Neither archive contains production secrets, user databases, PDFs, logs, caches, or uploads.

## Branch flow

1. Create a feature worktree from `main`.
2. Make small, reviewable commits and pass the pull-request gate.
3. Integrate into `main` and pass the integration gate.
4. The release coordinator fast-forwards `production` to that exact checked `main` commit and pushes it. Force-push is forbidden.
5. Read the exact live release id from `/api/runtime` (`app_release.id`). During the one-time legacy migration, use the exact `DEPLOYED_SHA` value if the runtime still reports `development`.
6. Run a local build rehearsal before making a server connection:

   ```powershell
   pwsh -File deploy/release.ps1 -ExpectedLive '<exact-live-id>' -DryRun -KeepArtifact
   ```

7. Run the same command without `-DryRun` only after approval. The builder rejects a dirty tree, any branch other than `production`, and any commit not equal to `origin/production` before connecting to the server.

## Transaction guarantees

- The archive is generated from the Git commit, never from working-tree files.
- The archive, remote upload, candidate service, and immutable release directory use a full commit id, UTC timestamp, and random suffix.
- `release.json` binds the archive to the exact commit, its expected parent release, build time, and deterministic source-tree hash.
- The server holds `/run/lock/marx-search-release.lock` from the first live-version read through validation, cutover, marker update, and ledger append.
- A changed live id causes an immediate compare-and-swap failure. An older queued session cannot overwrite a newer release.
- A candidate is compiled, smoke-tested, started on port 8001, and checked on the runtime, home, pricing, AI, and reader endpoints before Caddy changes.
- `current` and `previous` are atomically replaced symlinks. A failed primary restart restores the direct predecessor and its service configuration.
- The ledger is append-only at `/opt/marx-search/release-ledger.jsonl`; the newest ten release directories are retained, with `current` and `previous` always protected.

## Audited rollback

Rollback is not a reverse deployment. Supply both identities explicitly:

```powershell
pwsh -File deploy/rollback_release.ps1 `
  -ExpectedCurrent '<current-release-id>' `
  -TargetRelease '<target-release-id>'
```

The server takes the same global lock, verifies the current id and immutable target, restarts and probes the target, restores the original release if health fails, and appends a rollback event to the ledger.

## Version agreement check

After a successful release, these four identities must agree:

- `/api/runtime` → `app_release.id`
- `/opt/marx-search/current/release.json` → `release_id`
- the last successful entry in `/opt/marx-search/release-ledger.jsonl`
- the commit reachable from the approved production tag/branch

`data_version` remains a separate corpus identity and must not be compared as an application release.

## Retired production paths

Incremental source upload, source patching, direct corpus replacement, and the
two historical corpus blue/green scripts now exit before changing production.
They cannot safely operate on the immutable `current/app` layout. Corpus
configuration changes must be committed and released with the application;
shared database/PDF/index changes require a future dedicated data transaction
using the same global lock. The watchdog and the explicit backup restore path
also acquire that lock before restarting or stopping the service.
