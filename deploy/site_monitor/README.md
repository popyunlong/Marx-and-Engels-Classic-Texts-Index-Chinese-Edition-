# Low-impact website monitor

This is the independently deployed `mazhu-monitor`, not the website application.
Baseline source was exported from the running container on 2026-11-07. Secrets,
monitor reports, and screenshots are deliberately excluded.

## Coverage and safety limits

Scheduled at minute 17 and 47 with 0–120 seconds of jitter. The 03:17 Asia/Shanghai
slot runs the daily profile instead of light coverage. No startup check or catch-up.
Light: page/form/account/configuration checks only; no checkout or AI generation.
Daily additionally makes one search, one chapter lookup, one exact page-image request.
Image decoding proves the image response, not complete PDF canvas/UI correctness.

All supported entry points use `scripts/locked-run.sh`: a shared flock and a 250s
process-group timeout. `schedule-state.json` claims attempts before work starts:
minimum 28 minutes between starts and one daily attempt per Shanghai date.
CLI `node src/runAll.js --mode=light --no-alert` is subject to the same limits.
`--only` restricts coverage; it never bypasses rate limits or triggers alert changes.

Network gate: 2 requests in flight, starts at least 300ms apart, 80 requests/180s
for light and 120/240s for daily. Redirect hops count. Stage-based same-origin
allowlist blocks all covers, external resources, polling, writes (except login and
the single daily search), downloads, websockets, prefetch and service workers.
CSS/JS responses are cached explicitly within one run (4MiB cap); Playwright
interception disables the browser's ordinary HTTP cache, so context reuse alone
is not claimed as resource caching. Source timing is used only when provided by
Server-Timing cfOrigin. Missing timing is counted, not inferred from network delay.

Host load >=4 or available memory <2GiB skips the round. 429/503, network errors,
deadline/budget exhaustion and origin time >2s prevent subsequent checks. Existing
errors are retained. Failed checks are not retried in the same round.

Reports retain history and include version/mode/status/coverage/network metrics.
Skipped/degraded entries are not green. Transient failures need two completed
scheduled opportunities, 5xx notify immediately. Alert delivery is bounded and
lastAlert advances only on confirmed success. No real alert is sent by offline tests.

## Deployment gate

Do not deploy this checkout directly. Commit/review/push the dedicated feature
branch and merge to main; package a clean pushed main commit. Production application
source/upstream/service is outside this change. Monitor-only deployment requires
the user's explicit authorization for updating/recreating `mazhu-monitor`.

Under `/run/lock/marx-search-release.lock`, verify the expected container image and
source hashes, wait for its active round to finish (do not run a check), back up its
source/config/alert and scheduling state with root-only permissions, and retain the
old image ID. Tag the existing image as the build baseline and build with this
Dockerfile using `--pull=false --network=none --build-arg BASE_IMAGE=<baseline-tag>`.
Stage the package in `/root/mazhu-monitor/releases/<commit-id>`; reuse existing
dependencies. Check disk capacity before any potentially large image write; do not
pull or prune images. Candidate runtime startup/version inspection must not launch
the daemon or make website requests.

Use the existing compose project label with this compose.yaml and an immutable
MONITOR_CANDIDATE_IMAGE tag. Replace only service `monitor` using `up -d --no-deps
--no-build monitor`; preserve the two existing report/screenshot mounts and .env.
Do not restart Docker or touch other containers. Verify CPU quota=0.5, memory=1GiB,
image/source identity and next scheduled time by inspection, not an active probe.

If the new monitor misbehaves, pause ONLY this monitor rather than automatically
reactivating the bursty old schedule. Record expected-current and target image IDs
under the release lock for any approved rollback. Preserve all old reports and
rollback evidence. Observe the next light and first daily run from existing logs;
do not silently claim overnight verification before it happens. Any repeated
website acceptance scenario requires separate explicit user authorization.

## Offline verification

Use the existing Playwright installation via NODE_PATH, with the installed Chrome
executable in MONITOR_BROWSER_EXECUTABLE. `node --test tests/offline.test.js` only
starts loopback fixtures; it never uses the production URL, credentials, mail or AI.
Set TEMP/TMP and MONITOR_TEST_ARTIFACTS to task-owned D-drive directories. The task's
ledger records each attempted suite, including failures; do not rerun without
explicit user authorization. Linux flock/process-group supervision requires a
separate isolated OS fixture if it has not been covered by the local Windows run.
