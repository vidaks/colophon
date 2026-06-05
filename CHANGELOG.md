# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **EPUB inspection for low-confidence mis-seeds** — when `resolve` lands below the
  confidence gate (or finds no match), it now inspects the book's own EPUB and
  re-adjudicates with two extra signals: the OPF `dc:identifier` ISBN (a real ISBN that
  resolves to a same-author Hardcover book heals deterministically, no LLM —
  `source=epub-opf`), and the colophon / copyright page text folded into the Haiku prompt
  (`source=epub-colophon`). Only below-threshold books pay for the file I/O. Gated on the
  new `COLOPHON_BOOKS_ROOT` (the host path the library mounts from); unset ⇒ feature off.
  New module `colophon/epub.py` (stdlib-only) plus `hardcover.book_by_isbn` and
  `grimmory.epub_path` helpers.
- **Acquisition-side `verify`** — a read-only `verify(requested, file)` (new module
  `colophon/verify.py`; CLI `colophon verify <file> --hcid <id>` / `--title`) answering
  *is this downloaded file the requested work?* at work granularity, for the
  identifier-verified acquisition gate (plan 22). Deterministic path: the file's embedded
  OPF ISBN → Hardcover work, compared to the requested work via the shared `canonical_id`.
  No resolvable ISBN: the file is identified through the resolver's adjudicator (search by
  the file's own title/author, fold in its colophon text — the LLM only ever selects a real
  candidate) and that work is compared to the requested one; below `_LLM_MIN` (0.8)
  confidence the file is held as `unverifiable`. Reuses `epub` + `hardcover` + `matcher` +
  `resolver`; no writes, no grimmory DB at runtime. Exits 0/3/4 (match/mismatch/unverifiable).
  `epub.inspect` now also returns `opf_author` (dc:creator).

## [0.2.0] — 2026-06-04

### Added
- **`maintain` command** — runs backfill + resolve in a single process (each phase
  guarded so one failing doesn't skip the other), writes a one-page summary, and with
  `--email` always sends it as a daily heartbeat — including on an aborted run. Exits
  non-zero on a phase failure (so a supervising timer is marked failed and retries); an
  SMTP failure does not change the exit code.
- **Resolve skip-list** — mis-seeds that resolve to no-match or a below-threshold match
  are remembered (`resolve_skip` table) and not re-queried on later runs, cutting the
  repeated Hardcover + LLM calls for permanently-unresolvable books. The skip key is the
  normalized title+author, so a book is re-queried automatically once its metadata
  changes. `resolve --force` re-checks everything; `resolve --clear-skips` forgets the
  list; `COLOPHON_RESOLVE_RETRY_DAYS` (default `0` = never) sets an optional re-check TTL.

### Changed
- `resolve` reports now show the cached-unresolvable set, surfacing standing
  below-threshold matches with a one-line manual-apply command.

## [0.1.0] — 2026-06-03

Initial public release.

### Added
- **Backfill** — heal books with a correct provider id but a broken/missing ISBN by
  setting the canonical ISBN + locking + refreshing.
- **Resolve** — LLM-adjudicated re-identification of mis-seeded books over validated
  provider candidates (Claude Haiku via the Anthropic API or the `claude` CLI); auto-applies
  only above a confidence threshold.
- **Series audit** — compare series numbers against the provider's authoritative position
  and heal genuine mismatches (read-only by default).
- **Dedup** — collapse duplicate records via attach-to-keeper (loser's file preserved as an
  alternative format; empty record removed).
- **Oversight** — weekly changelog review (oscillation + error-rate verdict) that emails
  only when flagged.
- Precondition gate (files never touched), SQLite changelog with `revert`, dry-run default,
  per-run limits, and a circuit-breaker.
- Standard-library-only; configurable entirely via environment variables.

[Unreleased]: https://github.com/vidaks/colophon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vidaks/colophon/releases/tag/v0.2.0
[0.1.0]: https://github.com/vidaks/colophon/releases/tag/v0.1.0
