# Colophon

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/vidaks/colophon/actions/workflows/ci.yml/badge.svg)](https://github.com/vidaks/colophon/actions/workflows/ci.yml)

**An autonomous library-metadata healer for [Booklore](https://github.com/booklore-app/booklore)-family
book servers.** Colophon finds books whose metadata is wrong — broken ISBNs, the wrong
edition, a mis-identified work, a bad series number, duplicates — and fixes them by
resolving the *correct identity* and letting the server re-derive everything else. It
runs hands-off on a schedule, never touches your book files, logs every change, and
uses a small LLM only as a *validated adjudicator*, never as an author of record.

> **Heads-up:** Colophon writes to (and can delete records from) a live library. It is
> **dry-run by default**; `--apply` is what writes. Read [Safety](#safety) before you use it.

---

## Why it exists

Book servers import files with whatever metadata is embedded — often a foreign edition,
an ASIN-as-title, or a half-right match. The fix is almost never "edit fields by hand";
it's "pin the book to its *correct edition* and let the server refill from there." Colophon
automates exactly that: **resolve the canonical identity (ISBN + provider id), lock it,
trigger a refresh** — and the server fills in title, series, number, author, cover. The
lock pins the edition, so a healed book *stays* healed (set-once, no oscillation).

## What it does

- **Backfill** — books with a correct provider id but a broken/missing ISBN → set the
  canonical ISBN, lock, refresh.
- **Enrich** — bare imports that arrive with *no* provider id → submit a missing-only
  refresh so they seed. Books that can never match (no/odd ISBN, self-published editions
  the provider lacks) are remembered after a few failed sweeps, dropped from the sweep, and
  surfaced **once** in the daily summary with a link — delete or keep, your call.
- **Resolve** — mis-identified books (title disagrees with the matched work). Searches
  the metadata provider for candidates and asks an LLM to pick the *same work* — but only
  from the retrieved set, validated against it (the model can never invent an id), and
  only auto-applies above a confidence threshold. Below it, or on doubt: left flagged.
- **Series audit** — compares each book's series number against the provider's
  authoritative position and heals genuine mismatches (read-only by default).
- **Dedup** — collapses duplicate records, moving the loser's file onto the keeper as an
  *alternative format* (nothing deleted from disk) and removing the empty record.
- **Oversight** — a weekly changelog review that flags drift (a book healed more than
  once = convergence failing; sustained error rate) and emails you *only when flagged*.

## How it works (the mechanism)

The server matches editions by **ISBN** and re-derives metadata from a provider on
refresh. So Colophon's whole job is to set the *right* identity and get out of the way:

1. `PUT /books/{id}/metadata` — set `isbn13` + the provider book id, and **lock** them.
2. Trigger `REFRESH_METADATA` (`REPLACE_ALL`, refresh covers) — the server refills
   title / series / number / author / pages / cover from the locked edition.

Wrong metadata is almost always a *wrong edition*; pinning the canonical edition fixes
the rest in one move. The lock makes it converge.

## Requirements

- A running Booklore-family server (developed against the **grimmory/Edda** fork) reachable
  over its REST API, with an admin identity.
- A [Hardcover](https://hardcover.app) API key (the metadata provider).
- *(optional, for `resolve`)* An [Anthropic](https://www.anthropic.com) API key — or the
  `claude` CLI — for the LLM adjudicator (default model: Claude Haiku).
- Python **3.9+**. Standard library only — **no third-party dependencies.**

## Install

```bash
pipx install git+https://github.com/vidaks/colophon       # or: pip install .
# or just run it in place — there are no dependencies:
git clone https://github.com/vidaks/colophon && cd colophon
python -m colophon.cli --help
```

## Configure

Copy `.env.example` to `.env` and fill it in (or export the variables):

| variable | purpose |
|---|---|
| `GRIMMORY_URL` | server API base (default `http://localhost:6060/api/v1`) |
| `COLOPHON_ADMIN_USER` / `COLOPHON_ADMIN_GROUP` | admin identity used to mint a token |
| `COLOPHON_HARDCOVER_KEY` | Hardcover API key (or `COLOPHON_HARDCOVER_QUERY_CMD`) |
| `ANTHROPIC_API_KEY` | LLM adjudication (omit to use the `claude` CLI) |
| `COLOPHON_DB`, `COLOPHON_REPORTS` | where to write the changelog + reports |
| `COLOPHON_RESOLVE_RETRY_DAYS` | re-query a cached-unresolvable mis-seed after N days (default `0` = never) |
| `COLOPHON_BOOKS_ROOT` | host path of the library root; set it to let `resolve` inspect a below-threshold book's own EPUB (OPF ISBN + colophon). Unset ⇒ feature off |
| `COLOPHON_BOOKSTORE_URL` | public UI base; set it to deep-link each stuck book in the digest (route `/book/<id>`). Unset ⇒ listed without links |
| `COLOPHON_ENRICH_STUCK_AFTER` | mark a bare import stuck after N failed `enrich` sweeps (default `6`) |
| `SMTP_*`, `OVERSIGHT_TO` | oversight + `maintain --email` summaries |

## Usage

```bash
python -m colophon.cli precheck                 # assert the files-never-touched preconditions
python -m colophon.cli backfill                 # survey + propose (dry-run)
python -m colophon.cli backfill --apply         # heal broken ISBNs
python -m colophon.cli enrich --apply           # seed bare no-id imports; remember the unresolvable
python -m colophon.cli resolve --apply          # LLM-resolve mis-identified books (>= 0.9 conf)
python -m colophon.cli resolve --force          # re-query the cached-unresolvable mis-seeds
python -m colophon.cli series-audit             # series-number report (read-only)
python -m colophon.cli maintain --apply --email # backfill + resolve in one run + summary email
python -m colophon.cli oversight --days 7       # weekly changelog review + verdict
python -m colophon.cli log                      # the change history
python -m colophon.cli revert <run_id> --apply  # undo a metadata run
```

Every subcommand is **dry-run unless `--apply`** is given.

## Safety

Colophon is built to be trusted with an unattended, non-critical library — the design *is*
the safety net (there is no human approval gate):

- **Your book files are never touched.** Before any write it asserts the server's
  "save to original file" and "move files to library pattern" settings are **off**; if not,
  it aborts and writes nothing. Only the server's regenerable metadata/DB changes.
- **Dry-run by default.** Writes happen only with `--apply`.
- **Everything is logged** to a SQLite changelog, and metadata heals are **reversible**
  with `revert`.
- **The LLM never originates an identifier** — it only *selects* among provider candidates,
  and the choice is validated against that set. Low confidence ⇒ no change.
- **Bounded blast radius** — per-run limits + a circuit-breaker that stops on repeated errors.

⚠️ **Two things to know:**
1. **Dedup is not `revert`-able.** It moves a file and deletes a record; the loser's bytes
   survive as an alternative format on the keeper, but there is no changelog undo. Re-import
   if you need the separate record back.
2. **Data leaves your machine.** `resolve` and `series-audit` send book titles/authors to
   Hardcover and (for `resolve`) Anthropic. Don't run it on data you can't share with them.

Provided **as is**, without warranty (see [LICENSE](LICENSE)). Understand the above and keep
backups.

## Portability

The metadata intelligence — candidate-search → LLM adjudication → validated, reversible,
set-once writes → drift oversight — is generic. The server-specific integration lives behind
one thin seam (`colophon/grimmory.py`): the REST/DB calls and the lock-then-refresh premise.
Targeting a different Booklore-family server (or another book manager) means reimplementing
that seam; the rest is reusable.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). This is a personal project maintained on a
best-effort basis. Security reports: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Colophon is an independent program that talks to the server over its network
API; it is **not** a derivative of Booklore/Edda and carries no copyleft obligation.
