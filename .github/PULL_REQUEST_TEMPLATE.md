<!-- Thanks for contributing! Keep PRs focused — one concern each. -->

## What & why

<!-- What does this change, and why? Link any related issue (Fixes #…). -->

## Checklist

- [ ] Discussed in an issue first (for non-trivial changes)
- [ ] No new runtime dependencies (standard library only)
- [ ] Preserves the safety model (dry-run default, files never touched, LLM only
      adjudicates over validated candidates, every write logged)
- [ ] `python -m compileall colophon` passes; `ruff check .` is clean
- [ ] Updated `README.md` / `CHANGELOG.md` if behavior changed
- [ ] Tested against a real server with **dry-run** (no `--apply`); output pasted below

## Notes / test output

<!-- Paste dry-run output. Do NOT include secrets or full library dumps. -->
