# HTTPS access recovery, 2026-10-10

## Findings and limits

- Production release at investigation: `d422aeab0570b6cd5306d0656e6e25fc2f94aac7-20261008T133009Z-0134920a`. The current `origin/main` and `origin/production` commits were both `d422aeab0570b6cd5306d0656e6e25fc2f94aac7`.
- Public apex IPv4 and IPv6 connections served a valid Google Trust Services certificate and HTTP 200. Public `www` presented a valid edge certificate but returned Cloudflare 525 consistently. HTTP `www` redirected to HTTPS `www`, which also failed.
- The completed 30.1-minute local baseline (2026-10-10 03:59:05–04:29:11 UTC) made 496 checks across four Cloudflare addresses (two IPv4 and two IPv6): apex HTTPS 124/124 HTTP 200 with verified TLS; `www` HTTPS 124/124 HTTP 525 with verified edge TLS; HTTP for both hostnames 124/124 HTTP 301. The machine-readable record is `D:\CodexData\outputs\https-access-20261010\baseline\summary.json`. This is one network and cannot establish reachability in every region.
- Caddy listened on origin port 443. The active site block contained only `mazhuzuojiansuo.com`; its valid Let's Encrypt certificate covered only that name. A local TLS handshake using `www.mazhuzuojiansuo.com` as SNI produced no certificate. This explains the observed `www` origin handshake failure.
- No `systemd-flushd` service was listed among relevant origin services; the observed user's self-issued certificate differs from both the public Cloudflare edge certificate and the origin Caddy certificate. Its source remains unverified. A successful public probe cannot rule out interception on that user's network or device.
- The live corpus had 614 distinct `source_file` values; 610 were on the live public manifest, with four unmatched Western Marxism paths. The current paths for *Marx and Engels Collected Works* volume 1 and second edition volume 44 were on the manifest. September 4 repository history used the same paths for those two volumes. The downloadable files in the user's screenshots were unavailable for byte-level inspection.
- A read-only query of the production corpus found ten pages containing “健康” in each of those two volumes. The new HTML/Word comparison test uses their actual PDF page numbers (volume 1: 150, 394, 410, 430, 431, 432, 435, 436, 439, 440; volume 44: 73, 212, 214, 216, 283, 298, 305, 311, 312, 314). Its citation text is synthetic, so the test establishes link construction and page targeting, not the historical exported file's exact content.
- Existing HTML and Word export code uses the same percent-encoded `viewer_url`. New exports now omit the link if its public source is not on the manifest while retaining the citation; the reader shows a useful message for an unavailable source. This does not repair previously downloaded files.
- Cloudflare dashboard inspection found an `@` A record to `38.76.174.234` and a proxied `www` CNAME to the apex, both with automatic TTL. The zone currently uses **Full**, not Full (strict), encryption. No Page Rules were configured before this change. WeChat's current verdict remains unavailable.

The read-only origin configuration snapshots and bounded public probe output are stored under `D:\CodexData\outputs\https-access-20261010`. They contain neither private keys nor user content.

## Deployed `www` 302 and rollback identity

The designated coordinator deployed the exact GET/HEAD Single Redirect in Cloudflare on 2026-10-10. Its rule ID is `3d091d3afd094d1d8e89168a748db61f`, name `marx_www_to_apex_20261010`, and status is **302**. The pre-change dashboard record and deployed settings are under `D:\CodexData\outputs\https-access-20261010\snapshot`. This batch did not alter DNS, the apex rule, Caddy, HSTS, or encryption mode.

The first post-change probe verified all four Cloudflare addresses: apex HTTPS stayed HTTP 200 and `www` HTTPS became HTTP 302; HTTP `www` also returned 302. A path with encoded file, page, Chinese query, space and plus parameters preserved the complete query string in `Location`. Following the redirect reached apex HTTP 200 in one hop. A POST to `www` was not redirected and retained the pre-existing origin handshake failure. The completed 30-minute observation (2026-10-10 13:34–14:04 UTC) made 496 checks across all four IPv4/IPv6 edge addresses with zero TLS or HTTP failures: apex HTTPS 124/124 HTTP 200, apex HTTP 124/124 HTTP 301, and both `www` protocols 124/124 HTTP 302 each. Its bounded record is under `D:\CodexData\outputs\https-access-20261010\post-www-monitor`. Do not promote to 301 until at least 24 hours of healthy observation.

To roll back this batch, acquire `/run/lock/marx-search-release.lock`, verify the rule still has the exact ID and settings above, and disable or delete only `3d091d3afd094d1d8e89168a748db61f`. Probe both hostnames again. If another operator has changed the rule, stop and inspect rather than deleting by name alone.

## Independent `www` repair

Cloudflare's Single Redirect phase is the smallest change because the edge can redirect before connecting to the origin. Do not change the apex rule, Caddy upstream, DNS proxy mode, HSTS, or SSL/TLS mode to address this issue.

Create one enabled rule, after inspecting existing redirect precedence and confirming the `www` DNS record is proxied:

| Setting | Value |
| --- | --- |
| Match | `(http.host eq "www.mazhuzuojiansuo.com" and http.request.method in {"GET" "HEAD"})` |
| Dynamic destination | `concat("https://mazhuzuojiansuo.com", http.request.uri.path)` |
| Preserve query string | Yes |
| Initial status | 302 |
| Managed reference | `marx_www_to_apex_20261010` |

`scripts/cloudflare_www_redirect.py plan` prints the exact rule without credentials. Its `inspect`, `apply`, `promote`, and `rollback` commands use `CF_ZONE_ID` and `CLOUDFLARE_API_TOKEN`. Mutations also require a new, private `--backup-dir` under `D:\CodexData\outputs`; the tool saves the previous redirect ruleset and refuses to overwrite an existing snapshot. It appends one rule rather than replacing the ruleset. For rollback, pass the exact managed `--expected-rule-id` returned by `inspect`; rollback deletes only that rule. The tool holds `/run/lock/marx-search-release.lock` on `marx-cloud` while making a production change. Only the designated release coordinator should run it.

After applying 302, verify both IPv4 and IPv6 on HTTP and HTTPS. Check `/` and a path plus query such as `/viewer?file=pdfs%2Ftest%2Ba.pdf&page=73&q=%E5%81%A5%E5%BA%B7`; assert that the redirect's `Location` uses the apex and preserves the raw path and query. The test URL is for redirect inspection only and need not return a valid reader page. Confirm POST is not redirected, then observe for 24 hours. Only the exact managed 302 rule may be promoted to 301. Revert just this rule if apex health, TLS, or redirects degrade.

## Application release

The application change is independent of the Cloudflare rule. Use the dedicated `codex/https-access-hardening` worktree, the fast and integration gates, and a reviewed merge to `main`. Only the designated coordinator may fast-forward `production` and use `deploy/release.ps1` from a clean checkout exactly equal to `origin/production`. Read the live release ID before building. The production transaction holds `/run/lock/marx-search-release.lock`, checks a candidate, and drains existing requests. For rollback, use `deploy/rollback_release.ps1` with the expected current and target release IDs.

Before cutover, check application memory, swap activity, disk space, and current AI streams. Abort if the candidate cannot be run without pressure or if it fails any health or export link gate. Keep old releases and backups under the repository retention rule.

## Validation and continuing observation

- Verify HTML and Word exports for the current volume 1 and volume 44 paths, including Unicode and page parameters, against the same allowlist the reader uses.
- Verify that an unmatched source remains in the exported citation but has no dead link; missing or malicious source paths must still return 404 without disclosing an internal path.
- Run the usual login, membership, search, reader, AI stream and release gates. Do not use a real payment to test this change.
- On the Windows workstation, the focused HTTPS/export/security suite passed. The repository's full fast gate includes Linux shell release tests and cannot pass under this host's Docker-only WSL installation; use the pull-request Linux CI gate before any merge or release.
- Keep public probe output bounded with `scripts/probe_https_access.py`: maximum eight files of 6 MiB and seven-day age cleanup. It logs status, timing, destination address and certificate metadata only, with no response body, cookies or URL queries. Compare against the pre-change baseline. For failed TLS validation it records the error rather than sending HTTP traffic over the untrusted connection.
- Check two independent networks where available. A repeat of `systemd-flushd` or other untrusted certificate on public probes blocks a site-side "fully resolved" claim and calls for network-path investigation.
- Treat WeChat's safety warning separately. Check the actual URL and site content with a maintainer account, then use the platform's review channel if it still blocks the link. Record the submission and outcome separately from HTTPS verification.

Release gates: any verified certificate error or key workflow regression stops rollout. Two probes failing twice consecutively, or a five-minute 5xx rate more than one percentage point above baseline with at least five additional failures, triggers rollback of the current batch. A response-time median above twice baseline for ten minutes pauses the next batch and starts resource investigation. Continue five-minute checks for seven days after each production change.
