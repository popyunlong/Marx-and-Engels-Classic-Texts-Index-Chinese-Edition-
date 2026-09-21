# Compatibility-first modularization roadmap

The production baseline already exposes `create_app()` as the compatibility
factory, but route registration and service construction still live in the
1 MB `app.py`. Further extraction starts only after the immutable release path
has completed two healthy production releases. Each extraction commit must be
behavior-neutral and independently reversible.

## Extraction order

1. Search and reading blueprints: standard search, associative search, viewer,
   page rendering, table of contents, and library browsing.
2. AI and citation blueprints: chat, research review, citation assistant,
   quota accounting, and provider adapters.
3. Account, membership, and payment blueprints.
4. Journal ingestion, alerting, storage, and delivery blueprints.
5. Administration and operational diagnostics.

## Service boundaries

- Corpus loading and lookup receive configured data/PDF roots; routes do not
  mutate module globals.
- Release identity and runtime status remain separate from `data_version`.
- File storage, access policy, quota, payment, and external AI services are
  injected explicitly at blueprint registration time.
- Inline page JavaScript moves one template at a time into a matching static
  module, with a page-level regression snapshot added before the move.

## Commit rules

- Preserve URL, permission, response schema, database, and rendered-page
  behavior in extraction commits.
- Do not combine a module move with a new feature or schema change.
- Run the full Python 3.10 suite, the 3.11 compatibility gate, application
  smoke checks, and a candidate/rollback rehearsal after each blueprint group.
