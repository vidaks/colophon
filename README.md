# Colophon

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/vidaks/colophon/actions/workflows/ci.yml/badge.svg)](https://github.com/vidaks/colophon/actions/workflows/ci.yml)

Colophon is an automated metadata tool for ebook servers in the [Booklore](https://github.com/booklore-app/booklore) family, including the grimmory/Edda fork. It finds books with incorrect or missing metadata, resolves their canonical identity on [Hardcover](https://hardcover.app), and locks the verified fields so the server can refresh title, author, series, and cover art.

Colophon runs unattended on a schedule, never alters original book files, logs every change to a local SQLite database, and can revert previous updates. When matching ambiguous titles, it can optionally use a small language model to select among retrieved Hardcover search results. The model only chooses from real candidates and never invents identifiers.

All commands run in dry-run mode by default. Changes are written only when you pass the `--apply` flag.

---

## Why it exists

Ebook managers often import files with incomplete or inaccurate embedded metadata. An import may have a missing ISBN, a foreign edition, or an ASIN listed as a title.

Fixing these issues by editing individual text fields by hand takes time and easily breaks on the next metadata sync. Colophon takes a different approach: it identifies the canonical edition of the book, writes the verified ISBN and Hardcover ID to the server, and locks those fields. Colophon then requests a metadata refresh from the server. The server retrieves the correct title, author, series details, page count, and cover image from Hardcover. Because the identity fields remain locked, subsequent library scans preserve the corrected data.

## Features

- **Backfill:** Finds books that already have a Hardcover ID but lack a valid ISBN. It fetches the canonical ISBN, locks the record, and triggers a refresh.
- **Enrich:** Triggers initial metadata lookups for newly imported books that lack a provider ID. Books that fail to match after several attempts are marked as stuck and listed in a summary report for manual review.
- **Resolve:** Identifies books where the local title disagrees with the current provider match. Colophon searches Hardcover for candidates and can use a language model (such as Claude Haiku) to select the correct edition from the returned results. Updates only apply when the match meets a high confidence threshold.
- **Series audit:** Compares series titles and volume numbers against Hardcover series data. It corrects missing volume numbers, fixes mismatched numbers, and assigns ungrouped books to their series.
- **Duplicate audit:** Finds potential duplicate books in the library and highlights a recommended copy to keep based on file size and import date. Colophon only reports duplicates; it never deletes or merges records automatically.
- **Oversight:** Inspects the changelog weekly to detect repeated updates to the same book or elevated error rates. It sends an email notification only if an issue requires attention.

## How it works

Booklore-based servers match book editions by ISBN and retrieve details from an external provider when refreshed. Colophon uses this design to fix metadata in two steps:

1. Send a `PUT /books/{id}/metadata` request to set the ISBN-13 and Hardcover IDs, and lock those fields.
2. Trigger a `REFRESH_METADATA` job with the `REPLACE_ALL` mode and cover refresh enabled.

The server then repopulates all remaining metadata from the locked edition.

## Requirements

- A running Booklore-family server (such as the grimmory/Edda fork) with an accessible REST API and admin credentials.
- A [Hardcover](https://hardcover.app) API key.
- Python 3.9 or newer. Uses the standard library only, with no third-party runtime dependencies.
- *(Optional, for `resolve`)* An [Anthropic](https://www.anthropic.com) API key, or the `claude` command-line tool, for automated candidate matching.

## Installation

Install using `pipx` or `pip`:

```bash
pipx install git+https://github.com/vidaks/colophon
```

You can also run Colophon directly from a cloned repository without installing dependencies:

```bash
git clone https://github.com/vidaks/colophon
cd colophon
python3 -m colophon.cli --help
```

## Configuration

Copy `.env.example` to `.env` and configure the settings for your environment:

| Variable | Description |
|---|---|
| `GRIMMORY_URL` | Base URL of the book server API (default: `http://localhost:6060/api/v1`) |
| `COLOPHON_ADMIN_USER` / `COLOPHON_ADMIN_GROUP` | Admin username and group used for authentication |
| `COLOPHON_HARDCOVER_KEY` | Hardcover API key (or use `COLOPHON_HARDCOVER_QUERY_CMD`) |
| `ANTHROPIC_API_KEY` | Anthropic API key for candidate resolution (optional if using `claude` CLI) |
| `COLOPHON_DB`, `COLOPHON_REPORTS` | Storage paths for the SQLite changelog and generated reports |
| `COLOPHON_RESOLVE_RETRY_DAYS` | Number of days before retrying previously unmatched books (default: `0`, never) |
| `COLOPHON_BOOKS_ROOT` | Host path to the ebook library. When set, `resolve` can inspect local EPUB files for metadata if online matching confidence is low |
| `COLOPHON_BOOKSTORE_URL` | Public web address of the book server. Used to generate links to books in summary reports |
| `COLOPHON_ENRICH_STUCK_AFTER` | Number of failed match attempts before marking an import as stuck (default: `6`) |
| `SMTP_*`, `OVERSIGHT_TO` | SMTP server settings and recipient address for status emails |

## Usage

Run any command with `--help` to inspect its options:

```bash
python3 -m colophon.cli --help
```

Common commands:

```bash
# Verify that server settings prevent direct file modifications
python3 -m colophon.cli precheck

# Preview missing and broken ISBN fixes (dry-run)
python3 -m colophon.cli backfill

# Apply ISBN fixes to the server
python3 -m colophon.cli backfill --apply

# Request initial metadata lookup for newly imported books
python3 -m colophon.cli enrich --apply

# Resolve misidentified books using Hardcover search and LLM adjudication
python3 -m colophon.cli resolve --apply

# Audit series names and volume numbers against Hardcover
python3 -m colophon.cli series-audit

# Run backfill, resolve, and series audits in one pass and email the summary
python3 -m colophon.cli maintain --apply --email

# Review recent changelog entries for errors or repeated updates
python3 -m colophon.cli oversight --days 7

# View recent metadata changes
python3 -m colophon.cli log

# Revert metadata changes made in a specific run
python3 -m colophon.cli revert <run_id> --apply
```

All commands operate in dry-run mode unless you pass `--apply`.

## Safety

Colophon is designed for unattended operation with strict safeguards:

- **Original files remain untouched:** Before writing any changes, Colophon verifies that server settings for saving metadata to original files and moving files into library folder patterns are turned off. If either setting is enabled, Colophon aborts immediately. All updates affect only the server database and cache.
- **Dry-run by default:** Operations simulate changes and display proposals unless you pass the `--apply` flag.
- **Audit log and rollback:** Every metadata update is recorded in a local SQLite changelog. You can inspect previous actions with `log` and revert changes with `revert`.
- **Restricted matching:** When using a language model to resolve ambiguous books, the model can only choose among candidates retrieved from Hardcover. It cannot invent new identifiers. If match confidence is below the configured threshold, Colophon makes no changes.
- **Rate limiting and error limits:** Execution halts automatically if repeated errors occur during a run.

Keep two considerations in mind:

1. **Reverting changes:** The `revert` command restores metadata updates applied during `heal`, `backfill`, `resolve`, and `series-audit` runs. The `enrich` command only triggers an initial server metadata lookup for unidentified books and does not record prior state, so it cannot be undone with `revert`.
2. **Third-party data:** Commands like `resolve` and `series-audit` send book titles and author names to Hardcover and Anthropic. Do not run these commands on libraries containing sensitive titles you do not wish to send to external APIs.

## Architecture and Portability

Colophon keeps its matching logic separate from server communication. All interactions with the book server live in `colophon/grimmory.py`, which handles authentication, metadata updates, and database queries. Adapting Colophon to support another book manager requires implementing that module for the new server's API.

## Contributing

Contributions and issue reports are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for details. Report security concerns according to [SECURITY.md](SECURITY.md).

## License

This project is licensed under the [MIT License](LICENSE). Colophon is an independent program that communicates with book servers over their network APIs; it is not a derivative work of Booklore or Edda.

