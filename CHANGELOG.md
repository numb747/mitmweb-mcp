# Changelog

## 0.2.0

### Breaking

- **`get_content`'s `max_bytes` argument is now `body_max`.** The name promised
  bytes but `_clip` counts characters, so on a CJK or emoji-heavy body the cap
  understated the real size by up to 3x — in exactly the "this body is too
  large" case the argument exists for. `body_max` is also what the same argument
  is called on `inspect_flow`, `diff_flows`, `generate_code` and `replay_flow`.

  If your MCP client is an AI agent, there is nothing to do: clients read
  `tools/list` when they connect, and upgrading this package restarts the
  server, so the new name is picked up automatically. Hand-written call sites
  that pass `max_bytes=` do need editing — and as of this release such a call is
  rejected by name rather than ignored, so it fails loudly instead of quietly
  receiving the 20000-character default.

- **An unrecognised argument is now an error on every tool.** FastMCP builds its
  argument models with pydantic's default policy for unknown keys, which is to
  ignore them, so `list_flows(limmit=5)` returned 30 rows and said nothing. An
  agent can recover from an error; it cannot recover from a wrong answer it has
  no reason to doubt. Misspellings that used to pass now raise and name the
  offending argument.

- **`get_content(which=...)`, `search_flows(scope=...)` and
  `generate_code(framework=...)` now reject invalid values before the call runs**
  rather than raising from inside it. The accepted values appear in the schema
  as an enum. Valid calls are unaffected.

- **`list_flows(limit=0)` and `search_flows(limit=0)` are now rejected** instead
  of returning one row. The cap was tested after the row was appended.

- **Minimum `mcp` is now 1.14** (was a nominal 1.2 that had never actually
  worked — until 1.14, importing this module raised `TypeError` from
  `Tool.from_function`). Minimum `pydantic` is 1.14's own floor, 2.11.

### Added

- Every tool argument now carries a description in the JSON Schema — 35 of 35,
  up from 0. The per-argument prose moved out of the tool descriptions, so those
  got shorter rather than longer.

- Every tool now publishes MCP annotations. The nine read-only tools declare
  `readOnlyHint`, so a client can decide on its own to run them without
  prompting; `replay_flow` declares `destructiveHint` instead, because it
  replays whatever was captured and a captured `DELETE` still deletes. The
  boundary was previously documented for humans only.

- `tests/check_tool_contract.py` and `tests/check_chrome_bypass.py`, both run in
  CI, plus a `floor-check` job that installs the declared minimum `mcp` and
  verifies the server still comes up on it.

### Fixed

- `contrib/chrome.py`: with `chrome_capture_localhost=true`, only
  `127.0.0.1:<ui-port>` was added back after `<-loopback>` removed Chrome's
  implicit bypasses. Bypass rules match the host as written in the URL, so
  opening the UI as `http://localhost:8081/` went through the proxy and every
  click appended a flow. All loopback spellings are now restored, IPv6 literals
  are bracketed, and a wildcard bind no longer emits an unmatchable rule.

- `contrib/chrome.py`: `mitmdump -s contrib/chrome.py --set
  chrome_capture_localhost=true` raised `AttributeError` and never launched
  Chrome, because `web_port`/`web_host` are registered by mitmweb, not by core
  mitmproxy. Their absence is now read as "no web UI to exclude".

## 0.1.0

Initial release.
