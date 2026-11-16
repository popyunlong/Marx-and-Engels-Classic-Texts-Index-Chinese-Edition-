# happy membership — 2026-11-16

## Product behavior

- `support_happy`: happy 会员, CNY 666 cents / 30 days, no automatic renewal.
- Uses the existing new-membership purchase group, multi-month discounts,
  queued same-tier renewal and prorated upgrades. Rank 5 preserves higher
  existing/legacy memberships; display order 100 precedes Basic.
- Excludes AI research conversation (quick and research modes), citation Agent
  including compatibility routes, domestic/foreign research updates and
  personal library. Other ordinary member features inherit existing defaults.
- No paid AI wallet. Reader AI uses the registered 20,000 token weekly pool
  and Flash non-thinking model; buying/renewing does not reset usage.
- Independently purchased wallets remain valid without unlocking excluded
  features. Refunded upgrades retain their audit rows but a happy wallet is
  never exposed as usable paid entitlement.
- Cloud conversation eligibility and retention omit happy subscriptions.
  Existing records remain readable/deletable under the existing policy;
  earlier qualifying memberships retain their original grace period.
- Existing administrator overrides and runtime availability switches remain
  in effect. Journal membership remains a fixed rule excluding happy.

## Validation ledger

Base: `c20baddba9bdb417287c2e27d0b2f83a6f50efb9`.
Branch: `codex/happy-membership-20261116`.
Artifacts: `D:\CodexData\test-artifacts\happy-membership-20261116`.

The same-work no-repeat rule applies across subsequent chats/context windows.
Read `ledger.json`, the JUnit XML and source hashes before any additional test.

| Execution | Scope | Result | Evidence |
| --- | --- | --- | --- |
| Initial pytest run | Happy cases, membership tiers, quota resets, citation routes, four security cases, release fast gates | 45 passed; 2 failed; 37 setup errors | `pytest.log`, `pytest.xml`, `source-hashes.json` |
| JavaScript syntax | `static/ai-page/ai-page.js` | Passed once | `javascript-syntax.log`, `ledger.json` |
| Authorized targeted rerun + new collision case | 37 prior setup errors + 2 prior failures; 1 new case | 39 passed; 1 failed | `pytest-retest-1.log`, `pytest-retest-1.xml`, `source-hashes-retest-1.json` |
| Authorized legacy coexistence rerun | Legacy tier and administrative renewal selection, with no additional paid quota | 1 passed | `pytest-legacy-coexistence.log`, `pytest-legacy-coexistence.xml`, `source-hashes-legacy-coexistence.json` |

Initial setup errors came from the test fixture switching SQLite paths without
resetting `admin_store._SCHEMA_READY`. This has been fixed. The Agent preview
copy again preserves the existing deployment-gate wording while explaining
happy's exclusion. An order-number collision in the old membership test is
addressed by checking/retrying the suffix within the existing write transaction;
a new deterministic collision case has been added.

The user explicitly authorized the 39-case rerun once. That rerun plus the new
collision test passed 39 cases. The remaining coexistence case exposed a real
ranking mismatch: legacy subscriptions use interval months (1/3/12), not their
stored tier ranks. Selection now explicitly puts happy below other valid
memberships; the administrative renewal selector uses the same ordering and
ignores future queued new subscriptions.

The user authorized one rerun of the failed coexistence case. It passed on
the ranking correction. Earlier passing results apply to their recorded
source hashes. The full suite has not been run on the final revision.

## Delivery

The dedicated checkout is under `D:\CodexData\workspaces\happy-membership-20261116`.
Reuse the existing Python environment; no new dependency environment or corpus
download is required. Tests use synthetic data in D-drive temporary directories.
No production database, payment gateway or email service is changed by this work.
Production publication remains with the designated release coordinator under
the existing locked release transaction and required gates.

## Production candidate

The deployment changes are ported to upstream baseline
`b6d3a9bec332bc35dc0aa9a18c3afd33f7fbd738`. The older 15/25-yuan packages
are retired on this baseline, so happy upgrades to the current Plus package.
REST/MCP is included at standard queue priority. The shared research access
policy covers the new research-updates pages and subscriptions, and the
all-members mailing query skips happy-only subscribers.

The first run of the new research-updates web, subscription and mailing
scenario passed. See `current-research-module-first-run.xml` and
`source-hashes-release-port.json` in the artifact directory. Earlier results
belong to the source revisions in the test ledger.

### Single authorization for the release

| Stage | Tests and evidence | Maximum |
| --- | --- | --- |
| Local before commit | Happy checkout, renewals, upgrades/refunds, rights, account, research mail, AI quota, legacy coexistence; pricing copy regression | One pytest invocation of `test_happy_membership.py` and the pricing test in `test_auto_site_text.py` |
| Feature push | Repository pre-push compile, deploy manifests, release metadata, server smoke | Each once |
| Pull request | CI fast for Python 3.10 and 3.11, browser, API/MCP | One workflow run, each matrix job once |
| Main merge | CI fast matrix, browser, API/MCP, full pytest, server smoke, reproducibility check | One workflow run. Reproducibility builds two packages |
| Production fast-forward | Production pre-push compile, deploy manifests, release metadata, full pytest, server smoke | Each once |
| Production CI | Same jobs as main, plus lineage and release metadata rehearsal | One workflow run |
| Release | Dry-run and formal immutable build. Each compiles once and verifies the local manifest before/after compilation. Formal transaction also stages and verifies the archive, compiles the server candidate and runs server smoke | One dry-run, one formal transaction |
| Candidate and live | Verify ¥6.66/30 days, purchase entry, research sample, expected runtime identity, and mandatory catalogue/browser evidence; verify release ledger and current metadata after cutover | One candidate and one live acceptance round. The transaction retains its built-in warmup, 30-minute paired observation, health polling and recovery checks |

The list includes automatic repeat checks across workflows and within a
workflow (deterministic vs. full pytest, and the additional production-lineage
metadata test). These gates must run on the rebased code and the exact
production commit. Prior authorizations covered only earlier targeted runs.
One explicit authorization is needed for this list and for designation of the
current session as the release coordinator. A failed or interrupted stage
does not authorize another execution.

The user confirmed the one-time authorization. The local gate passed all
38 happy cases. The pricing-copy regression failed on the wording for the
day-15 quota release. The page now retains the expected wording and limits
that allowance to Plus, Pro and Max. The existing authorized main integration
run includes this pricing regression and gates production advancement. See
`release-local-authorized.xml` and the corresponding ledger entry.
