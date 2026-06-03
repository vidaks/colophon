# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/vidaks/colophon/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vidaks/colophon/releases/tag/v0.1.0
