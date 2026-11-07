# Validation evidence — 2026-11-07

Source baseline: running monitor image
`sha256:f6fa5cb573983495ecaca4bd55bd3917eed19467f0132e741ebce80e3b5a394e`.

- Original offline suite: five logic tests passed; browser fixture failed because
  its HTML omitted UTF-8. Original failure retained.
- First explicitly authorized browser-only retry: first six checks passed; chapter
  fixture script had a quoting error. Reader was skipped; failure retained.
- Generated AI/search/chapter inline script syntax validation: passed once after
  correcting the chapter fixture.
- Second explicitly authorized browser-only retry: passed all eight checks,
  including chapter and exact page-image decoding. Only this scenario was rerun.
- Three additional cases passed once: cookie isolation in static cache,
  redirect/busy-origin shutdown, and preventing false recovery of legacy AI/payment
  full-chain alerts after coverage changes.
- Isolated Linux supervision fixture passed once: shell syntax, cross-process
  flock exclusion/release, timeout process-group termination. No Docker alteration
  or website request was made by this fixture.

Detailed attempt records, source hashes and loopback request logs are retained at
`D:/CodexData/outputs/remote-slowness-20261107/monitor`.
No real credentials, mail, order creation or model calls were used by the fixtures.

This delivery changes only the independently deployed monitor. Website application
regressions are outside the change. Existing pre-push website Python/deployment
tests and automatic whole-website CI are not repeated: targeted monitor checks
above supply the relevant fast gate, with every failed attempt retained. A skip-CI
commit marker prevents automatically duplicating unrelated tests at PR and merge.
No production application branch or application release is advanced by this change.

Website end-to-end acceptance has not been repeated. Passive observation of the
first new scheduled light and daily rounds remains pending until those rounds run.
