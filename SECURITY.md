# Security Policy

## Supported versions

This is a personal project; only the latest `main` is supported. Fixes land there.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

- Use GitHub's **[Report a vulnerability](https://github.com/vidaks/colophon/security/advisories/new)**
  (Security → Advisories), or
- open a minimal public issue asking for a private contact channel — without details.

Expect a best-effort response. There is no bounty.

## Security model & expectations

Colophon holds credentials and writes to a live library. The design keeps the blast
radius small, but operators carry real responsibility:

- **Secrets live only in the environment.** API keys (Hardcover, Anthropic) and the SMTP
  password are read from environment variables (see `.env.example`) and are never inlined
  in the code, written to the changelog, or printed. Keep `.env` out of version control
  (it is git-ignored) and `0600`.
- **Admin token / server exposure.** Colophon mints an admin token from the server's
  remote-auth endpoint using a trusted-header identity. That endpoint trusts whoever can
  reach it, so the **server's API port must not be exposed** to untrusted networks — bind
  it to loopback / a trusted bridge and front it with your auth proxy. Treat the minted
  token as a secret for its lifetime.
- **Third-party data flow.** `resolve` and `series-audit` send book titles/authors to
  Hardcover and (for `resolve`) Anthropic. This is inherent to the feature; do not run it
  on data you cannot share with those providers.
- **Destructive operations.** `--apply` writes; `dedup` deletes records (file preserved as
  an alternative format, but not changelog-revertible). Run dry-run first; keep backups.

Reporting a way to bypass the files-never-touched precondition, leak a secret, or escalate
the minted token beyond intended scope is especially appreciated.
