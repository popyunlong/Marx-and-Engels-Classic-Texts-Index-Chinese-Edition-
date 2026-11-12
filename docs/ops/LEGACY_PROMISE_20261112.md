# July legacy AI promise restoration

Baseline: remote main/production `12d8406feb3ac4ca756b757672b16f50826382dd`.
Feature: `codex/legacy-promise-20261112`, existing isolated D-drive worktree.

The protected payment cutoff is Beijing 2026-07-04 00:00 (UTC July 3 16:00), independent of the August reform cutoff. Active protected monthly/quarterly/yearly subscriptions have 280k/490k/770k actual prompt+completion tokens per Beijing calendar week. No monetary cap is imposed on this promise. Only Flash non-thinking is funded. Web gifts cover the first 20 successful/reserved daily tool slots, never model tokens; separately purchased rights fund additional authorized usage. Highest applicable daily limit wins, not the sum.

Qualification requires a direct paid order, a reviewed mapping, or a unique exact historical zpay callback match (same user, plan, paid_at=created_at). No nearest-payment heuristic. Gift ancestry requires a unique same-tier contiguous subscription chain; later purchases do not inherit. Unknown links are reported for review. Startup preserves the matching evidence in `legacy_ai_order_links`; it never alters payment history.

`legacy_ai_reservations` marks site-funded preauthorizations without wallet allocations. Actual provider costs remain in the normal provider/tool ledgers. Pre-cutover reservations settle to their original wallets. Replayed reserved request IDs reuse the reservation; completed IDs cannot start a second call. A provider completion for a reservation is recorded once. Existing in-flight historical reservations temporarily retain their stored estimate; actual provider tokens replace that estimate on settlement. No spending history or week usage is reset.

Read-only production audit at 2026-11-12T04:50:48Z found 25 eligible users (12 quarterly, 13 yearly), 2,540.55 remaining member-days, no unresolved active subscription links. Recorded last-30-day non-mascot model cost for this cohort was ¥1.234243 and 610,664 tokens; tools ¥0. This includes separately purchased rights and is not a future usage forecast. Holding the cohort constant for 30 days, full utilization costs ¥292.92 at 80% input/20% output peak pricing, or ¥619.80 at all-output peak pricing, including web. These are scenarios, not total contractual liabilities; current prices are Flash ¥2/M input, ¥8/M output and ¥0.005/tool call.

## Evidence and no-repeat policy

Prior records reviewed: `docs/ops/MEMBER_WEB_TEST_LEDGER_20261112.json`, `D:/CodexData/outputs/member-web-20261112/release-status.json`, backend/edge/browser originals and authorized retests, PR/main/production gate records. Their successful results apply to the prior release, not this change.

New scenarios live in `tests/test_legacy_promise.py` and `tests/browser/legacy_promise.cjs`; guarded runner `scripts/check_legacy_promise_once.ps1` writes immutable attempt markers, hashes and results under `D:/CodexData/outputs/legacy-promise-20261112`. Existing gates and failed new cases must not be rerun without scope-specific authorization. See the adjacent JSON ledger for current status.

First backend attempt: 20 passed. Two additional, previously unexecuted cutover/late-settlement cases: 2 passed. Browser first attempt: failed at the 390px horizontal-overflow assertion after account rendering, actual API entitlement checks, Flash-only selection and continuation had passed. The long actual-token label inherited `white-space: nowrap`. A scoped `actual-tokens` wrapping rule now addresses this; browser repair remains unverified pending specific retest authorization. No old test or release gate has been rerun for this change.

## Release and rollback

Use the normal checked main → clean remote-matching production → release.ps1 transaction and release lock. Re-read ExpectedLive immediately before releasing. The migration is additive and wallets/history remain intact. Drain requests before either cutover or rollback so an old process does not settle a new site-funded reservation. Rollback restores prior wallet eligibility behavior; preserve new accounting tables and never restore an old database over newer orders. Candidate acceptance must include protected member rights, comprehensive continuation and existing chapter/API entrypoints. No production mutation has occurred during implementation/audit.
