# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-18

First public release. Not yet verified against a live MAX account — see
[docs/protocol-notes.md](docs/protocol-notes.md).

### Added

- MCP server for a personal MAX messenger (max.ru) account on top of PyMax (`maxapi-python==2.5.0`).
- 20 read tools: chats, folders, history by date range with cursors, messages around a point,
  messages by id, chat activity timeline, message search (history scan), chat/contact search,
  unread summary (does not mark anything as read), channel post comments, members, users, contacts,
  link resolution without joining, attachment info, media download, event waiting, reactions.
- 17 write tools, registered only with `MAX_READ_ONLY=false`: send message/file, forward, edit,
  delete, react, mark read, polls (create/vote), pin, channel comments, group create/add/remove/update,
  join (also needs `MAX_ALLOW_JOIN=true`), leave.
- Login via QR (terminal and SVG), web session token, SMS (mobile client) and 2FA password file;
  CLI `max-mcp` / `max-mcp-server`: `serve`, `login`, `status`, `logout`, `tools`, `http-token`, `doctor`.
- Transports: stdio and Streamable HTTP (loopback only by default, mandatory bearer token,
  Host/Origin checks, body size limit, optional secret path).
- Security: read/write/deny chat allowlists, global request rate limit and send limit,
  `<untrusted>` wrapping of third-party text, SSRF-safe media downloader with host allowlist and
  IP pinning, upload directory confinement, session file 0600 in a 0700 directory, profile lock,
  log redaction, write audit log, PyMax telemetry/remote catalog/auto re-login disabled.
- Tests without network access (network guard), contract tests of the PyMax adapter on synthetic
  JSON fixtures, CI (ruff, pytest on Python 3.11–3.13, gitleaks) and PyPI Trusted Publishing release workflow.

[Unreleased]: https://github.com/artgas1/max-mcp-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/artgas1/max-mcp-server/releases/tag/v0.1.0
