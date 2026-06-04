# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
