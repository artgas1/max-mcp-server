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

### Security

Fixes from an independent pre-release security review (no critical or high findings).

- `format=compact` history: one `<untrusted>` wrapper per message; the author marker (`[me]` for the
  owner, `[id<N>]` otherwise) is placed outside the wrapper, so a display name such as "я"/"[me]" or a
  message containing a fake line can no longer impersonate the owner. All line separators
  (including U+2028/U+2029, NEL, VT, FF) become `⏎`. With `MAX_UNTRUSTED_WRAP=json` compact output
  is a list of `{"h": header, "untrusted": body}`.
- Text normalization strips every invisible format character (category Cf except ZWJ): Unicode Tags
  ("ASCII smuggling"), soft hyphen, invisible operators, deprecated format chars, interlinear
  annotations, variation selectors 1–14 and U+E0100–E01EF. Escaping of `<untrusted>`/`</untrusted>`
  now runs after normalization and also catches case/NFKC variants, fullwidth brackets, HTML
  entities, Cyrillic look-alikes and arbitrary non-letter insertions.
- Logs: tracebacks are printed without the exception message and source lines (file, line,
  function and exception type only) and the whole formatted record is redacted; pymax, websockets,
  aiohttp, httpx, httpcore stay at WARNING or above even with `MAX_LOG_LEVEL=DEBUG`; bare Russian
  phone numbers (`7XXXXXXXXXX`) are masked. Full tracebacks only with `MAX_LOG_PYMAX_DEBUG=1`.
- `max_create_group` now requires `MAX_ALLOW_CREATE_GROUP=true` or `MAX_WRITE_CHATS=*`; an empty write
  allowlist forbids it like every other write.
- Usernames, chat invite links, reactions and string fields of events are wrapped as untrusted;
  downloaded files get server-chosen names (`<chat>_<msg>_<idx>_<kind>.<ext>`); `ValueError` texts from
  PyMax (pydantic payload excerpts) are no longer returned to the model.
- Load limits: at most 20,000 scanned messages per call, `MAX_HEAVY_CONCURRENCY` (default 2) concurrent
  heavy calls, `MAX_TOOL_TIMEOUT` (default 300 s) per heavy call, bounded LRU cache for `max_chat_timeline`.
- `status --probe` takes the profile lock and does not connect while `serve` runs; `doctor` reports
  whether `serve` is running.
- The QR SVG is deleted after login, timeout or Ctrl-C, with a warning for directories wider than 0700.
- Secret files (login token, 2FA password) are opened with `O_NOFOLLOW` and checked via `fstat`.
- With a non-empty `MAX_DENY_CHATS`, events without a `chat_id` are dropped.
- HTTP: the bearer token is checked before the request body is buffered; a warning is logged when
  listening off loopback without TLS. README: TLS proxy / SSH tunnel recipe, `--insecure-no-auth`
  never on a shared server, deny list hides chats not people, hard links in `MAX_UPLOAD_DIR`.
- Media downloads use a unique `O_EXCL` temporary file.
- Dependencies are capped at the next major version; `aiohttp>=3.12.14`.
- Release workflow: only tags on `main`, no uv cache in the release build.

[Unreleased]: https://github.com/artgas1/max-mcp-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/artgas1/max-mcp-server/releases/tag/v0.1.0
