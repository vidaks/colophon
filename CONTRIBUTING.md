# Contributing

Thank you for your interest in contributing to Colophon.

## Ground Rules

- Open an issue before starting work on non-trivial changes so we can discuss the approach.
- Maintain the zero-dependency constraint. Colophon relies strictly on the Python standard library; new runtime dependencies require strong justification.
- Respect the safety invariants: dry-run by default, never modify book files on disk, constrain LLM choices to verified candidates, and record every update to the changelog.
- Keep pull requests focused on a single change.

## Development Setup

Colophon requires no build step and installs no third-party runtime dependencies:

```bash
git clone https://github.com/vidaks/colophon
cd colophon
cp .env.example .env
python3 -m colophon.cli --help
```

Always test against your own server in dry-run mode (without `--apply`) to preview proposals safely.

## Code Style and Verification

- Follow PEP 8 guidelines: 4-space indentation, descriptive naming, and comments explaining why decisions were made.
- Run tests and static checks before opening a pull request:

  ```bash
  python3 -m compileall colophon
  ruff check .
  python3 -m unittest discover -s tests -v
  ```

- Update documentation and `CHANGELOG.md` when introducing user-facing changes.

## Reporting Issues

- Bug reports and feature suggestions: open an issue on GitHub.
- Security concerns: report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

Contributions are licensed under the [MIT License](LICENSE).
