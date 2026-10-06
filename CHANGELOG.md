# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Initial metadata enrichment with tracking:** Added the `enrich` command to request initial metadata lookups for newly imported books lacking a provider ID. Failed attempts are recorded in `store.enrich_state`. Books that repeatedly fail to match are marked as stuck and listed once in the daily summary report for manual review.
- **EPUB file inspection for low-confidence matches:** When online title search confidence falls below threshold, `resolve` can inspect local EPUB files to read OPF metadata identifiers and colophon copyright page text, improving match accuracy. Enabled by setting `COLOPHON_BOOKS_ROOT`.
- **Ebook verification command:** Added `verify` (`colophon/verify.py`), a read-only tool that verifies whether a downloaded ebook file matches a requested title or Hardcover identifier. Returns match, mismatch, or unverifiable status.

### Changed
- **Automated test execution in CI:** Added the `unittest` test suite step to GitHub Actions workflows across Python 3.9 through 3.12.
- **Storage path configuration:** Corrected handling of `COLOPHON_DB` and `COLOPHON_REPORTS` environment variables so configured paths override default locations. Consolidated report directory creation into a single helper.

### Fixed
- **Database connection management:** Fixed SQLite connection handling in `store.py` to close connections properly after commits, preventing open file descriptor leaks.
- **Authentication error handling:** Improved error messages for authentication and proxy handshake failures with the server API.
- **Deduplication documentation:** Corrected documentation in the README and changelog to clarify that Colophon only audits and reports duplicate books; it does not delete or merge records automatically.

## [0.2.0] — 2026-06-04

### Added
- **Consolidated maintenance command:** Added `maintain` to run backfill and resolve passes together with unified summary reporting and optional email dispatch.
- **Skip list for unresolvable books:** Books that fail to match online are recorded in a skip list to avoid repeated queries on subsequent runs. Entries refresh automatically if book titles or authors change, or when using `--force`.

### Changed
- Summary reports now display cached unresolvable entries alongside manual command suggestions.

## [0.1.0] — 2026-06-03

### Added
- Initial public release.
- **Backfill:** Updates books with valid provider IDs but missing ISBNs, locking the identity and refreshing metadata.
- **Resolve:** Automated identification of misidentified books using Hardcover search and language model adjudication.
- **Series audit:** Compares series titles and volume numbers against Hardcover data and updates discrepancies.
- **Duplicate audit:** Scans for duplicate book entries and recommends preferred copies based on file size and import date.
- **Oversight:** Weekly changelog analysis reporting repeated updates or elevated error rates.
- Reversible SQLite change logging, dry-run mode by default, and safety checks verifying server file settings.

[Unreleased]: https://github.com/vidaks/colophon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vidaks/colophon/releases/tag/v0.2.0
[0.1.0]: https://github.com/vidaks/colophon/releases/tag/v0.1.0
