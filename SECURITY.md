# Security policy

## Supported versions

Only the latest released version receives security fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via GitHub Security Advisories:
[Report a vulnerability](https://github.com/artgas1/max-mcp-server/security/advisories/new)
(repository → Security → Advisories → "Report a vulnerability").

Include the version, configuration (environment variables **without** secrets), steps to reproduce
and the impact. **Never include** session tokens, `session.json`, HTTP tokens, phone numbers, real
chat names or message contents — replace them with placeholders.

You should get a first response within 7 days. Fixes are released as a patch version with a
GitHub Security Advisory; reporters are credited unless they prefer otherwise.

## Scope

In scope: anything that lets a third party (a message author, a web page, another local user or
process) read the session, read chats outside the configured allowlists, send or modify anything
in MAX without the owner's request, reach the network beyond MAX, or read/write files outside the
configured directories. Prompt-injection bypasses of the write allowlist and limits are in scope.

Out of scope: account restrictions by MAX for automated use (a documented risk of this unofficial
client), and issues that require write access to the session directory.

---

# Политика безопасности

Об уязвимостях сообщайте **приватно** через GitHub Security Advisories:
[Report a vulnerability](https://github.com/artgas1/max-mcp-server/security/advisories/new),
не через публичные issues. Не прикладывайте токены, `session.json`, телефоны, реальные названия
чатов и тексты сообщений — замените их заглушками. Первый ответ — в течение 7 дней.
