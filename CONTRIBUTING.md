# Contributing

Thanks for your interest. This is a small, personal project maintained on a
**best-effort** basis — issues and PRs are welcome, but response times vary and not every
change will fit the scope.

## Ground rules

- **Open an issue first** for anything non-trivial, so we can agree on the approach before
  you write code.
- Keep the **standard-library-only, zero-dependency** constraint. A new runtime dependency
  needs a strong justification.
- Respect the **safety model** (see the README): dry-run by default, files never touched,
  the LLM only adjudicates over validated candidates, every write logged. PRs that weaken
  these need to make the case explicitly.

## Dev setup

No build step, no dependencies:

```bash
git clone https://github.com/vidaks/colophon && cd colophon
cp .env.example .env          # fill in for live runs
python -m colophon.cli --help
```

Run against your own server; **dry-run (no `--apply`) is safe** and is how you should
develop and review changes — the audit/propose output *is* the test.

## Style & checks

- Follow the surrounding style (PEP 8, 4-space indent, descriptive names, comments for the
  *why*).
- CI runs `ruff` (critical-error rules) and a compile check across Python 3.9–3.12. Before
  opening a PR:

  ```bash
  python -m compileall colophon
  ruff check .        # optional locally; CI runs it
  ```

- Keep PRs focused; one concern per PR. Explain user-facing or safety-relevant changes in
  the description, and update the README / `CHANGELOG.md` when behavior changes.

## Reporting bugs & security

- Bugs / features: use the issue templates.
- Security issues: **do not** open a public issue — see [SECURITY.md](SECURITY.md).

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
