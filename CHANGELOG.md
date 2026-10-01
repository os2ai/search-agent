# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog], and this project adheres to [Semantic Versioning].

## [Unreleased]

- Added GitHub Actions for linting (ruff, markdown, YAML), tests and changelog checks.
- `/app/.venv/bin` is on the image `PATH`, so `ruff`, `pytest` and `basedpyright` run without `uv run`.
- `basedpyright` type checking: errors fixed, existing warnings accepted in `.basedpyright/baseline.json` so only new
  ones fail CI.
- Markdown and YAML reformatted to pass markdownlint and prettier.

## [0.0.5] - 2026-09-01

### Changed

- Upgraded to the mcp 2.x SDK (`MCPServer`); the `mcp_allowed_hosts` allowlist is now applied on the Streamable HTTP
  transport factory.
- Capped dependency major versions.
- Base image switched to the official Python image.

## [0.0.4] - 2026-07-15

No changes; re-tag of 0.0.3.

## [0.0.3] - 2026-07-15

### Added

- Pluggable search providers behind a `SearchProvider` protocol, selected with `SEARCH_AGENT_SEARCH_PROVIDER`: `searxng`
  (default) or the new `staan` (Staan "Web for AI") backend, which can return full page content or scored snippets per
  result.
- Staan content cap (`SEARCH_AGENT_STAAN_CONTENT_MAX_RESULTS`) enforced globally across all planner queries so the
  synthesizer prompt stays bounded.
- arm64 image support.

### Security

- SSRF check validated on every redirect hop and tightened; fabricated source URLs are dropped.
- Full query and context are no longer logged on the request path.
- Cached values are shape-validated, and request body and SearXNG response read sizes are capped.
- SearXNG secret is required.
- Patched transitive CVEs and blocked the compromised fastapi 0.136.3.
- Search providers hardened against malformed result items.

## [0.0.2] - 2026-05-05

No changes; re-tag of 0.0.1.

## [0.0.1] - 2026-05-05

### Added

- Initial implementation: FastAPI service with a 3-stage search pipeline (query planner, SearXNG search executor,
  combined analyze + synthesize agent) and `/health`, `/api/v1/search` endpoints.
- `search_web` MCP tool returning raw results for the caller's own citation handling.
- Configurable search counts, optional page fetch with trafilatura (markdown output) and DEBUG logging.
- Pluggable cache (`redis`, `memory`, `disabled`) for planner, search and fetch results.
- Better tool-call support for Mistral models and Open WebUI tool names.
- Non-root container support.
- README and Mozilla Public License 2.0.

[Unreleased]: https://github.com/AarhusAI/search-agent/compare/0.0.5...HEAD
[0.0.5]: https://github.com/AarhusAI/search-agent/compare/0.0.4...0.0.5
[0.0.4]: https://github.com/AarhusAI/search-agent/compare/0.0.3...0.0.4
[0.0.3]: https://github.com/AarhusAI/search-agent/compare/0.0.2...0.0.3
[0.0.2]: https://github.com/AarhusAI/search-agent/compare/0.0.1...0.0.2
[0.0.1]: https://github.com/AarhusAI/search-agent/releases/tag/0.0.1
[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
[Semantic Versioning]: https://semver.org/spec/v2.0.0.html
