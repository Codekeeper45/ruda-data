# RUDA — raw unenriched source snapshot

This branch preserves the source database from before the manual enrichment
and materialized audit. The SQLite file is published as the `app.db` asset of
release `raw-v1.0.0`; it is not stored as a Git blob.

- Kind: untouched pre-audit source snapshot
- Release: `raw-v1.0.0`
- Asset: `app.db`
- SHA-256: `5d065cf362996ea6c3146c4af7cbdb201c73a2f7dec3dd43b914950254aaf353`

This is byte-for-byte source preservation, not the production database. Its
42 inherited foreign-key warnings are recorded in the manifest and are fixed
only in the audited `master` release.
