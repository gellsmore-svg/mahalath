# Security Policy

Mahalath is an early local-first research prototype. Treat it as unsuitable for
untrusted network exposure until authentication, authorization, and data-handling
boundaries are explicitly designed and tested.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's
[private vulnerability reporting](https://github.com/gellsmore-svg/mahalath/security/advisories/new)
("Report a vulnerability" under the repository's **Security** tab). This keeps the
details private until a fix is available.

When reporting, please include the affected version/commit, a description, and
(ideally) a minimal reproduction.

## Handling credentials

Mahalath uses a MongoDB connection string and, optionally, an Anthropic Claude API
key for frontier-LLM review. Keep these in your local `config.yaml` (gitignored)
and scrub any connection strings or API keys from logs before sharing them in a
report.
