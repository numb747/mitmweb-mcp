# mitmweb-mcp

**One UI. You watch it, your AI reads it.**

[English](README.md) · [简体中文](README.zh-CN.md)

An [MCP](https://modelcontextprotocol.io) server that reads the **mitmweb session you
are already running** — the same flows, the same window, the same moment.

You keep the mitmweb UI open and drive the browser yourself. Your AI assistant sees
exactly what you see, and can search it, diff it, replay it, and turn it into a
runnable scraper.

```
                     ┌──────────────► browser UI      (you)
  browser ──proxy──► mitmweb (:8080 proxy / :8081 API+UI)
                     └──────────────► mitmweb-mcp ───► AI assistant
                          one process · one flow list
```

---

## Why this exists

Other mitmproxy MCP servers spin up their **own headless proxy**. That gives you two
separate capture sessions: the one you are looking at, and the one the AI is looking
at. To reconcile them you have to either chain the two proxies (longer path, two TLS
hops, doubled latency) or accept that the two views disagree.

mitmweb-mcp takes a different route. The insight is that **mitmweb's own frontend is
just an HTTP client** — the flow list you see in the browser comes from
`GET /flows.json`, and clicking into a body calls
`GET /flows/<id>/response/content.data`. That REST API has always been there; nobody
treats it as an API.

So this server starts no proxy at all. It is a second, parallel client of the mitmweb
you already have. Three properties fall directly out of that architecture, with no
synchronisation machinery needed:

1. **What the UI shows is what the AI reads.** One process, one in-memory flow list.
2. **Zero added latency.** Nothing is inserted into the request path. Your browsing
   feels exactly as it did before.
3. **If the MCP server dies, capture keeps running.** It is only a reader.

## Safety model: read-only and append-only

Nine of the ten tools are plain `GET` requests and cannot alter your session.
`replay_flow` does not modify existing flows either — it re-sends a request, which
appends a new flow. The worst case is a few extra rows; nothing you are looking at
can silently change or disappear.

This boundary is published, not just documented: the nine readers carry
`readOnlyHint: true`, so a client can decide on its own to run them without asking.
`replay_flow` is marked `destructiveHint: true` — it appends to *your* session, but
what it puts on the wire is whatever was captured, and replaying a `DELETE` deletes
something. It should be the one tool that stops and asks.

There is deliberately **no `clear_flows` tool**. Wiping the session is destructive and
irreversible, it is one click in the UI, and there is no reason to hand an AI that
button.

---

## Install

Requires **Python ≥ 3.10** and [mitmproxy](https://mitmproxy.org/) ≥ 10 on your PATH.

```bash
pip install mitmweb-mcp
```

Or from source:

```bash
git clone https://github.com/numb747/mitmweb-mcp
cd mitmweb-mcp
pip install -e .
```

## Setup

### 1. Start mitmweb with a fixed token

mitmweb generates a **random** web password at every launch, which this server has no
way to discover. Pin it:

```bash
mitmweb --listen-port 8080 --set web_password=YOUR_SECRET_TOKEN
```

- `8080` is the **proxy** port — point your browser here (replays go through it too)
- `8081` is the **UI + API** — you watch this, and so does the MCP server

Trust mitmproxy's CA once so HTTPS works. **Firefox** accepts the <http://mitm.it>
flow or an import of `~/.mitmproxy/mitmproxy-ca-cert.pem`. **Chrome on Linux does
not** — it reads the shared NSS database, so use `certutil` instead:

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n mitmproxy \
         -i ~/.mitmproxy/mitmproxy-ca-cert.pem
```

Optionally start with a cleaner view:
`--set view_filter='!~a & !~d googleapis.com'`. Note that `view_filter` applies to
`/flows.json` as well, so it narrows what this server sees, not just the UI.

[`contrib/`](contrib/) has a mitmweb addon that launches a dedicated Chrome
pointed at the proxy, plus `mitm-start` / `mitm-stop` scripts — optional, but it
turns the whole thing into one command.

### 2. Register the MCP server

**Claude Code:**

```bash
claude mcp add mitmweb -s user \
  -e MITMWEB_URL=http://127.0.0.1:8081 \
  -e MITMWEB_TOKEN=YOUR_SECRET_TOKEN \
  -e MITMPROXY_PORT=8080 \
  -- mitmweb-mcp
```

**Claude Desktop** (`claude_desktop_config.json`) **or any MCP client:**

```json
{
  "mcpServers": {
    "mitmweb": {
      "command": "mitmweb-mcp",
      "env": {
        "MITMWEB_URL": "http://127.0.0.1:8081",
        "MITMWEB_TOKEN": "YOUR_SECRET_TOKEN",
        "MITMPROXY_PORT": "8080"
      }
    }
  }
}
```

| Variable | Default | Must match |
|---|---|---|
| `MITMWEB_URL` | `http://127.0.0.1:8081` | mitmweb's `web_port` |
| `MITMWEB_TOKEN` | *(empty)* | mitmweb's `web_password` |
| `MITMPROXY_PORT` | `8080` | mitmweb's `--listen-port` |

Restart your MCP client, then ask it to run `status` to confirm the connection.

---

## Tools

| Tool | What it does |
|---|---|
| `status` | Connectivity check and flow count. Start here when debugging. |
| `flow_stats` | Hosts, status codes, asset ratio, hottest endpoints (numeric ids normalised to `{n}`). |
| `list_flows` | Recent flows, newest first. Filter by host, method, status, URL, content-type, **time window**, or **UI mark**. |
| `inspect_flow` | One flow in full: query params, both header sets, both bodies, latency, and a ready-to-run `curl`. |
| `get_content` | A complete body, gzip/brotli already decoded. |
| `search_flows` | Full-text search across all flows, optionally regex. |
| `diff_flows` | Compare two requests field by field. |
| `detect_auth` | Identify which auth schemes the site uses and where the credentials live. |
| `generate_code` | Emit a runnable scraper: `curl_cffi`, `httpx`, `requests`, or a shell script. |
| `replay_flow` | Re-send a request with browser TLS fingerprinting; optionally rewrite method, headers, or body. |

Flow ids can be the 8-character short form that `list_flows` returns — they are matched
by prefix.

### Three design details worth knowing

**Static assets are excluded by default.** A modern page produces hundreds of flows of
which maybe five matter. `list_flows` applies the equivalent of mitmproxy's `!~a`
filter unless you pass `include_assets=True`. Binary bodies are never decoded into
mojibake; you get `<binary image/png, 8090 bytes, omitted>` instead.

**Your UI actions are usable as input.** This is the payoff of sharing one session,
and no headless design can offer it:

- `list_flows(marked_only=True)` — mark a few flows in the mitmweb UI, and the AI
  analyses only those.
- `list_flows(since_seconds=15)` — you just clicked a button; this isolates exactly
  what that click triggered.

**Replay goes through your proxy.** mitmweb's native replay endpoint is protected by
Tornado's XSRF, whose cookie is only issued to the `/updates` websocket. Rather than
maintain a websocket for that, `replay_flow` re-sends the request through your own
proxy — so the result lands in your UI anyway, and you gain capabilities the native
replay does not have: **TLS/JA3 fingerprint impersonation** via
[curl_cffi](https://github.com/lexiforest/curl_cffi), arbitrary header and body
rewriting, and `allow_redirects=False` so every hop stays visible.

---

## A worked example

**1 — Find the endpoint behind something you can see**

> *"The order number SO20260910 is on this page. Which request returned it?"*

```
search_flows("SO20260910")   → POST /api/order/list
inspect_flow("a3f21b8c")     → signing headers, body shape, equivalent curl
```

**2 — Work out which parameters are signed**

Trigger the same action twice, then:

```
diff_flows("a3f21b8c", "c44a48f7")
```

```json
{
  "same_endpoint": true,
  "query_diff": { "changed": { "nonce": { "a": "aaa", "b": "bbb" } } },
  "body_diff":  { "changed": { "sign":    { "a": "1111", "b": "2222" },
                               "meta.ts": { "a": 1000,   "b": 2000   } } }
}
```

Identical fields are omitted, so what remains *is* the answer: the signature covers a
nonce and a timestamp. `page` and `meta.ver` never varied, so they are not part of it.

**3 — Confirm it reproduces outside the browser**

```
replay_flow("a3f21b8c")                     → same 200, no browser involved
replay_flow("a3f21b8c", body={"page": 2})   → probe paging and edge cases
```

Every replay appears in your UI as you go.

**4 — Ship it**

```
generate_code(["a3f21b8c"], framework="curl_cffi")
```

```python
#!/usr/bin/env python3
"""Generated by mitmweb-mcp from captured traffic.

Adapt as needed: add paging loops, concurrency, retries, error handling."""
from curl_cffi.requests import Session

IMPERSONATE = 'chrome'


def main() -> None:
    with Session(impersonate=IMPERSONATE) as s:

        # --- 1. POST /api/order/list (originally returned 200) ---
        r1 = s.post(
            'https://example.com/api/order/list',
            params={'page': '1'},
            headers={'Authorization': 'Bearer ...', 'Content-Type': 'application/json'},
            json={'page': 1, 'sign': '1111'},
        )
        print("1.", r1.status_code, r1.text[:200])


if __name__ == "__main__":
    main()
```

Pass several ids to generate a multi-step script — the requests share one `Session`,
so a "log in, then call the API" sequence carries its cookies across.

---

## Scope

This server is the **analysis** layer. Capture, live interception with breakpoints, and
clearing the session stay in the mitmweb UI, where they belong — interception in
particular is inherently interactive and there is nothing to gain from proxying it
through an AI.

For unattended bulk capture, use `mitmdump` with an addon script; that is a different
job from the one this tool does.

---

## Development

```bash
git clone https://github.com/numb747/mitmweb-mcp
cd mitmweb-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
python tests/test_e2e.py     # needs mitmproxy on PATH and internet access
```

The end-to-end test launches a real mitmweb on ports 18080/18081, pushes traffic with
known marker values through it (including the same endpoint called twice with only
`nonce`/`sign`/`ts` differing, to exercise `diff_flows`), then drives every tool over
a real MCP stdio session. It asserts **56 behaviours**, including that generated code
compiles and that `diff_flows` omits fields which did *not* change.

### Two things that will bite you

**`GET /flows/<id>` returns 405.** `/flows.json` is the only list endpoint and it
returns everything, at roughly 2.6 KB per flow. The 2-second TTL cache is therefore a
correctness-of-cost requirement, not a micro-optimisation. Similarly, `search_flows`
fetches bodies concurrently — serially it would be hundreds of round-trips.

**FastMCP pre-parses JSON string arguments.** A parameter annotated `str` that receives
a valid JSON string gets parsed into a dict *before* validation, which then fails with
`Input should be a valid string`. This is why `replay_flow` annotates `headers` and
`body` as `dict | str | None`.

## Contributing

Issues and pull requests are welcome. Please run `ruff check .` and the end-to-end test
before opening a PR.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built on [mitmproxy](https://mitmproxy.org/) and
[curl_cffi](https://github.com/lexiforest/curl_cffi).
