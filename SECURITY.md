# Security Policy

## Supported Versions

Only the latest commit on the `main` branch is actively supported. Security fixes land directly on `main`.

## Reporting a Vulnerability

Please report security issues privately rather than opening a public issue:

- Use GitHub's **[Report a vulnerability](https://github.com/vidaks/colophon/security/advisories/new)** feature under Security -> Advisories.
- Alternatively, open a minimal public issue requesting a private contact channel without disclosing vulnerability details.

## Security Model and Expectations

Colophon handles API credentials and performs updates on a live book server. Keep the following practices in mind:

- **Secrets in environment variables:** API keys (Hardcover, Anthropic) and SMTP credentials are read strictly from environment variables. They are never written to the database changelog, printed in logs, or committed to version control. Restrict `.env` file permissions to `0600`.
- **Server API exposure:** Colophon authenticates with the book server by requesting an admin token from the remote authentication endpoint using trusted headers. Because that endpoint trusts incoming requests, do not expose the book server's API port to untrusted networks. Bind the service to localhost or an isolated internal container bridge behind your authentication proxy. Treat generated session tokens as sensitive secrets.
- **Third-party data transfer:** Commands such as `resolve` and `series-audit` transmit book titles and author names to Hardcover and Anthropic APIs. Do not run these features on library catalogs containing sensitive data that you cannot share with third-party services.
- **Safe operation:** Always preview changes with dry-run mode before applying updates with `--apply`. Maintain regular backups of your book server database.
