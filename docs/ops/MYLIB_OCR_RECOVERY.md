# Private library bounded OCR: coordinator runbook

## Safety boundary

OCR stays on the member storage host (154.201.94.174). The existing storage,
MinIO, source PDFs, page rendering and conversation services are not restarted
or replaced. Only `/node/ocr/` is routed to the new loopback API. Its nginx
location inherits the existing server-level IP ACL and TLS configuration.
The main site's search, corpus, databases and Caddy routing are not changed by
the OCR component transaction. All main changes still require the ordinary
blue/green main release gates. Do not bypass failed or unauthorized gates.

The worker runs one book and one short-lived page process at a time. Its cgroup
uses MemoryHigh=3G, MemoryMax=4G, MemorySwapMax=0 and OOMPolicy=kill. The queue API
has a separate 512M limit. Before each page the worker requires 5GiB available;
below 1GiB during a page it terminates the page, preserves checkpoints, and
pauses following work. Each page has a 180-second deadline. A resource wait
expires after 30 minutes. There is no automatic page or job retry.

Each run needs one host-side admission. Dispatch is disabled until explicitly
enabled. Both are audited in the private shared job database. Existing runtime
dependencies are checked against `mylib_ocr/runtime.json`; never upgrade the
shared environment or download replacement models as part of this release.

## Implementation provenance

The layout algorithm was copied byte-for-byte from the member storage host:
`mylib_ocr_layout.py` sha256
`828c54207f62e843f5b7621672ae487a9ee538c32f1de3c8aedb448b100f777c`.
Original runner fingerprint:
`0d2cac88afacbc3c394b63b3639d669dcb6c3571fa0ee4fa50da2bf1049bbe2f`.
Original storage fingerprint:
`aec9273faafaa417af106c580fbd711daebb1a0b0d6d65f7d6160b775038c17a`.
No credentials or member documents belong in this repository.

## Publication order

1. Read the task's existing test and operation ledger. Obtain explicit approval
   for any repeated checks in local/PR/main/production CI before triggering them.
2. A designated coordinator records the predecessor main PID and publishes the
   guarded main revision through `deploy/release.ps1`. Startup reconciliation
   must not enqueue old tasks or audit old books. Candidate startup maintenance
   stays disabled by the existing main release controls.
3. After the predecessor process has drained and exited, publish the **same
   clean origin/production revision** with:

   ```powershell
   ./deploy/release.ps1 -Component mylib-ocr -ExpectedLive absent `
     -PreviousMainPid <recorded-predecessor-pid> -IdentityFile <existing-key>
   ```

   For later component releases replace `absent` with its exact current release.
   The coordinator helper holds the main-host global release lock and the node
   transaction holds its node-local release lock. It verifies main protocol v2,
   matching production source revision, inactive predecessor PID, immutable
   archive hashes, pinned runtime and effective service memory controls. It
   leaves OCR dispatch **paused**. Stop if any evidence is absent.
4. The member original namespace remains `mylib/<uid>/<sid>/source.pdf`. The new
   queue lives in `/opt/mylib-ocr/shared/jobs.sqlite3`; new completed candidates
   use `mylib/<uid>/<sid>/ocr-runs/<run_id>/text.jsonl`. Legacy OCR/text are never
   overwritten. Verify source SHA-256 before importing compatible v3 legacy
   pages; mismatches stop for review. Partial import is idempotent.
5. Only under the main global production lock, and only for a specifically
   authorized book, grant one admission as the existing service user:

   ```bash
   runuser -u minio-user -- /opt/mylib/venv/bin/python \
     -m mylib_ocr.control grant --uid <owner> --sid <submission> \
     --reason '<specific user authorization and operation ledger reference>'
   runuser -u minio-user -- /opt/mylib/venv/bin/python \
     -m mylib_ocr.control dispatch on
   ```

   Run these from the immutable current OCR release. Then use the authenticated
   admin **从断点继续OCR** action once. Record its main `run_id`, source SHA,
   queued response and actual persisted checkpoint pages. The old displayed
   progress is not proof of a durable checkpoint. Never use full rebuild.
6. Monitor this same task, not a new request. Its single production recovery is
   also the memory observation scenario. The main app may finalize and accept
   only a matching complete run, once. Stop the sequence after any interruption.
   Preserve quota and admission records; completed pages are not billed twice.

## Rollback and failure

Use `deploy/rollback_release.ps1 -Component mylib-ocr -ExpectedCurrent <current>
-TargetRelease <target> -PreviousMainPid <recorded-predecessor-pid>` under the
designated coordinator. The transaction pauses dispatch before switching.
`-TargetRelease absent` disables the new services and closes only the OCR nginx
path with HTTP503, preserving storage reads rather than reverting to unsafe
legacy OCR. A failed first promotion likewise leaves the OCR path closed.
Source files, checkpoints, private job audit and release events are retained.
Never clear the job database or force-requeue to make an error disappear.

If a run is interrupted, the UI reports failure while preserving its fingerprint
and checkpoint. Resource repair or code changes are not permission to retry it.
An interrupted finalization is never automatically repeated. The operator must
inspect the run-specific text/index/acceptance evidence before seeking any new
authorization. Worker startup interrupts former running jobs and pauses dispatch;
it never restarts them. Main startup only watches an existing active run ID.


## Complete offline OCR import

An administrator can import a complete local OCR bundle without starting the node
worker. Use the per-submission **导入本地OCR结果** form, with normal authenticated
admin session and management CSRF protection. This path never invokes node parse,
OCR start, or text fetch, and never replaces the original PDF or node sidecars.

The UTF-8 JSONL file has one manifest line followed by exactly `total` page lines
in PDF order, including successful empty-text pages. Maximum bundle size is
32MiB and each line is at most 1MiB. Manifest:

```json
{"type":"mylib-offline-ocr","format_version":1,"uid":1887,"sid":42,"source_sha256":"<64 lowercase hex>","total":350,"run_id":"<32 lowercase hex>","layout_schema":3,"engine":"rapidocr-layout-v3"}
```

Each following line uses the v3 page record emitted by `mylib_ocr.page`, with
`page`, `text`, `layout_schema`, `ocr_engine`, `ocr_profile`, `layout_blocks`, and
`folio_candidates`. Compatible saved pages use `ocr_profile=legacy-v3`; new
bounded pages use `bounded-page-v1`. `image_width` and `image_height` are optional
for legacy pages. Do not include images, PDFs, base64, credentials, or invented
publication metadata. Incoming `printed_pages` is ignored; sequence validation
recomputes folio evidence from the v3 candidates.

The main app checks owner, submission, source SHA, complete page range, schema,
normalized coordinates, consent, quota, and absence of active processing before
registering the run. It stores the exact input at
`personal_library/offline-imports/<uid>/<sid>/<run_id>.jsonl` and a globally unique
run receipt under `offline-imports/runs`. Both are private shared runtime data.
The receipt binds the bundle hash to its inputs. Duplicate input only returns the
original task; the same run ID with different input is rejected. An unknown or
interrupted outcome is never automatically replayed. Read-only admin status:
`GET /admin/library/submissions/<sid>/offline-import/<run_id>`.

Only the complete, matching run can write private derivatives and execute one
acceptance. Ordinary quality gates and metadata checks are unchanged. Main
startup marks interrupted offline work for inspection without starting a watcher
or accepting it again. Worker startup unconditionally pauses dispatch, including
when its predecessor left only queued jobs.
