#!/usr/bin/env python3
"""mitmweb-mcp — a thin-client MCP server that reads the mitmweb you are already running.

Design: this server starts no proxy of its own. You run mitmweb with its web UI as
usual; this server talks to the REST API that same mitmweb process already exposes.
The result is that what you see in the UI and what the AI reads are literally the
same in-memory flow list.

Safety boundary: read-only and append-only. Every analysis tool is a plain GET.
`replay_flow` never mutates an existing flow either — it re-sends the request through
your proxy, so the result shows up as a *new* flow in your UI.

That boundary is about *this* session, and it is published to clients as annotations:
the nine analysis tools declare `readOnlyHint`. `replay_flow` declares
`destructiveHint` instead, because the other end of the wire is not covered by any of
the above — it replays whatever was captured, and a captured DELETE still deletes.

Prerequisite — start mitmweb with a fixed token, otherwise it generates a random
password on every launch and this server cannot authenticate:

    mitmweb --listen-port 8080 --set web_password=<TOKEN>

Configuration is read from the environment (set these in your MCP client config):

    MITMWEB_URL     default http://127.0.0.1:8081  — the web UI/API address
    MITMWEB_TOKEN   must match the web_password above
    MITMPROXY_PORT  default 8080 — mitmweb's --listen-port, used when replaying
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

__version__ = "0.2.0"

MITMWEB_URL = os.environ.get("MITMWEB_URL", "http://127.0.0.1:8081").rstrip("/")
TOKEN = os.environ.get("MITMWEB_TOKEN", "")
# Replays are sent through your own proxy so they land in your UI like any other flow.
PROXY = f"http://127.0.0.1:{os.environ.get('MITMPROXY_PORT', '8080')}"

# mitmweb exposes exactly one list endpoint, /flows.json (GET /flows/<id> returns 405),
# and each flow costs roughly 2.6 KB. Re-downloading the whole list on every tool call
# is wasteful once a session has a few hundred flows, so we cache it very briefly.
# Two seconds is far below human interaction latency yet collapses the common
# "locate a flow, then fetch its body" pair into a single download.
FLOWS_TTL = 2.0
# search_flows may need several hundred bodies; fetching them serially is a non-starter.
FETCH_CONCURRENCY = 8

mcp = FastMCP("mitmweb-reader")


class Mitmweb:
    """Minimal read-only client for the mitmweb HTTP API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: tuple[float, list[dict]] | None = None
        self._sem: asyncio.Semaphore | None = None

    def _c(self) -> httpx.AsyncClient:
        # Created lazily: it must be constructed inside the MCP event loop, not at import.
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=MITMWEB_URL,
                timeout=20,
                params={"token": TOKEN} if TOKEN else None,
            )
        return self._client

    async def flows(self, *, fresh: bool = False) -> list[dict]:
        now = time.monotonic()
        if not fresh and self._cache and now - self._cache[0] < FLOWS_TTL:
            return self._cache[1]
        r = await self._c().get("/flows.json")
        r.raise_for_status()
        data = r.json()
        self._cache = (now, data)
        return data

    async def raw(self, flow_id: str, which: str) -> bytes | None:
        """Fetch a decoded body (mitmproxy already un-gzips/un-brotlis it)."""
        try:
            r = await self._c().get(f"/flows/{flow_id}/{which}/content.data")
            r.raise_for_status()
        except Exception:
            return None
        return r.content or None

    async def raw_many(self, jobs: list[tuple[str, str]]) -> list[bytes | None]:
        """Fetch many bodies concurrently, bounded by FETCH_CONCURRENCY."""
        if self._sem is None:
            self._sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(fid: str, which: str) -> bytes | None:
            async with self._sem:  # type: ignore[union-attr]
                return await self.raw(fid, which)

        return list(await asyncio.gather(*(one(f, w) for f, w in jobs)))


mw = Mitmweb()


# --------------------------------------------------------------------------- helpers

ASSET_EXT = (
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".avif", ".svg", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".mp4", ".webm", ".mp3", ".wav",
)
ASSET_CT = ("image/", "font/", "video/", "audio/", "text/css", "javascript")
# Content types we are willing to decode as text; anything else is treated as binary
# so that we return a short placeholder instead of pages of mojibake.
TEXTUAL_CT = (
    "text/", "json", "xml", "javascript", "html", "csv",
    "x-www-form-urlencoded", "graphql", "yaml",
)


def _headers_to_dict(pairs) -> dict:
    return {k: v for k, v in (pairs or [])}


def _ctype(msg: dict) -> str:
    ct = _headers_to_dict(msg.get("headers")).get("content-type", "")
    return ct.split(";")[0].strip().lower()


def _is_asset(f: dict) -> bool:
    """Equivalent to mitmproxy's ~a filter: static assets, almost always noise."""
    path = ((f.get("request") or {}).get("path") or "").split("?")[0].lower()
    if path.endswith(ASSET_EXT):
        return True
    ct = _ctype(f.get("response") or {})
    return any(x in ct for x in ASSET_CT) if ct else False


def _decode(data: bytes | None, ctype: str = "") -> str | None:
    if not data:
        return None
    if ctype and not any(x in ctype for x in TEXTUAL_CT):
        return f"<binary {ctype}, {len(data)} bytes, omitted>"
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Missing or misleading content-type, but the payload really is binary.
        if b"\x00" in data[:1024]:
            return f"<binary, {len(data)} bytes, omitted>"
        return data.decode("utf-8", "replace")


def _is_binary_placeholder(text: str | None) -> bool:
    return bool(text) and text.startswith("<binary")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...<{len(text) - limit} more characters truncated>"


def _url(req: dict) -> str:
    host = req.get("pretty_host") or req.get("host") or ""
    return f"{req.get('scheme')}://{host}{req.get('path') or ''}"


def _latency_ms(f: dict) -> int | None:
    start = (f.get("request") or {}).get("timestamp_start")
    end = (f.get("response") or {}).get("timestamp_end")
    return int((end - start) * 1000) if start and end else None


def _summarize(f: dict) -> dict:
    req, resp = f.get("request") or {}, f.get("response") or {}
    out = {
        "id": (f.get("id") or "")[:8],
        "method": req.get("method"),
        "host": req.get("pretty_host") or req.get("host"),
        "path": (req.get("path") or "")[:140],
        "status": resp.get("status_code"),
        "ctype": _ctype(resp) or None,
        "size": resp.get("contentLength"),
        "ms": _latency_ms(f),
    }
    # Only surface these when non-default, to keep the output scannable.
    for key in ("marked", "comment", "is_replay"):
        if f.get(key):
            out[key] = f[key]
    if (f.get("type") or "http") != "http":
        out["type"] = f["type"]
    return out


async def _find(flow_id: str) -> dict:
    """Locate a flow by full id or by the 8-character prefix that list_flows returns."""
    flows = await mw.flows()
    for f in flows:
        if f.get("id") == flow_id or (f.get("id") or "").startswith(flow_id):
            return f
    raise ValueError(f"No flow matching {flow_id!r} ({len(flows)} flows captured)")


async def _body_of(f: dict, which: str) -> str | None:
    msg = f.get(which) or {}
    if not msg.get("contentLength"):
        return None
    return _decode(await mw.raw(f["id"], which), _ctype(msg))


def _curl(req: dict, body: str | None) -> str:
    """Build a direct (non-proxied) curl command that can be pasted into a shell."""
    parts = ["curl", "-X", req.get("method") or "GET", shlex.quote(_url(req))]
    for k, v in req.get("headers") or []:
        if k.lower() not in ("content-length", "host", "connection", "accept-encoding"):
            parts += ["-H", shlex.quote(f"{k}: {v}")]
    if body and not _is_binary_placeholder(body):
        parts += ["--data-raw", shlex.quote(_clip(body, 2000))]
    return " ".join(parts)


def _as_dict(value: dict | str | None, label: str) -> dict:
    """FastMCP may pre-parse a JSON string argument into a dict, so accept both."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{label} is not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object, e.g. {{"X-Foo": "bar"}}')
    return value


def _diff_dict(a: dict, b: dict) -> dict:
    """Diff two flat dicts. Keys that are identical on both sides are omitted."""
    out: dict = {}
    if only_a := {k: a[k] for k in a.keys() - b.keys()}:
        out["only_in_a"] = only_a
    if only_b := {k: b[k] for k in b.keys() - a.keys()}:
        out["only_in_b"] = only_b
    if changed := {k: {"a": a[k], "b": b[k]} for k in a.keys() & b.keys() if a[k] != b[k]}:
        out["changed"] = changed
    return out


def _try_json(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _flat(d: dict, prefix: str = "") -> dict:
    """Flatten nested JSON to a.b.c keys so it can be diffed field by field."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, f"{key}."))
        else:
            out[key] = v
    return out


# ----------------------------------------------------------------------------- tools

# Every tool that takes a flow accepts the same thing, so the wording is shared rather
# than retyped — an agent should learn the id-prefix rule once.
FlowId = Annotated[str, Field(
    description="Flow id — either the full id, or the 8-character prefix that "
                "list_flows, search_flows and flow_stats hand back.",
)]


def _reads(title: str) -> ToolAnnotations:
    """Annotations for the nine tools that only read.

    Each is a plain GET against the mitmweb API: it cannot change the flow list and it
    never reaches past the local proxy. Stating that in annotations is what lets a client
    decide on its own to run these without asking, and to stop and ask about replay_flow.
    destructiveHint and idempotentHint are deliberately absent — the spec defines them as
    meaningful only when readOnlyHint is false.
    """
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=_reads("Check the mitmweb connection"))
async def status() -> str:
    """Check connectivity to mitmweb and report how many flows are captured.

    Start here when something is not working: on success it reports the UI address, the
    proxy address replays will use, and the flow count; on failure it returns the error
    plus the exact command to start mitmweb with a matching token.
    """
    try:
        flows = await mw.flows(fresh=True)
    except Exception as e:
        return json.dumps(
            {
                "connected": False,
                "error": f"{type(e).__name__}: {e}",
                "hint": "Make sure mitmweb is running and the token matches. Start it with: "
                        "mitmweb --listen-port 8080 --set web_password=<TOKEN>. "
                        f"Currently MITMWEB_URL={MITMWEB_URL}, "
                        f"token={'set' if TOKEN else 'NOT SET'}",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "connected": True,
            "url": MITMWEB_URL,
            "proxy": PROXY,
            "flow_count": len(flows),
            "version": __version__,
        },
        ensure_ascii=False,
    )


@mcp.tool(annotations=_reads("Traffic overview"))
async def flow_stats(
    top_endpoints: Annotated[int, Field(
        description="How many of the busiest endpoints to list. The rest are still "
                    "counted in the totals, just not enumerated.",
    )] = 10,
) -> str:
    """Overview of captured traffic: hosts, status codes, asset ratio, hottest endpoints.

    This is the first thing to run against an unfamiliar site: it tells you which host
    the interesting API lives on and whether anything is failing. Numeric path segments
    are normalised to {n}, so /user/1001 and /user/1002 are counted as one endpoint.
    """
    flows = await mw.flows(fresh=True)
    by_host: dict[str, int] = {}
    by_status: dict[str, int] = {}
    endpoints: dict[str, int] = {}
    assets = marked = 0

    for f in flows:
        req, resp = f.get("request") or {}, f.get("response") or {}
        host = req.get("pretty_host") or req.get("host") or "?"
        by_host[host] = by_host.get(host, 0) + 1
        status = str(resp.get("status_code") or "pending")
        by_status[status] = by_status.get(status, 0) + 1
        if f.get("marked"):
            marked += 1
        if _is_asset(f):
            assets += 1
            continue
        path = re.sub(r"/\d[\d\-]*", "/{n}", (req.get("path") or "").split("?")[0])
        key = f"{req.get('method')} {host}{path}"
        endpoints[key] = endpoints.get(key, 0) + 1

    return json.dumps(
        {
            "total": len(flows),
            "assets": assets,
            "api_like": len(flows) - assets,
            "marked": marked,
            "by_host": dict(sorted(by_host.items(), key=lambda x: -x[1])),
            "by_status": dict(sorted(by_status.items())),
            "top_endpoints": dict(sorted(endpoints.items(), key=lambda x: -x[1])[:top_endpoints]),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations=_reads("List flows"))
async def list_flows(
    # ge=1 because the cap is checked after the append: limit=0 would return one row.
    limit: Annotated[int, Field(
        ge=1,
        description="Stop after this many matching flows.",
    )] = 30,
    host: Annotated[str | None, Field(
        description="Keep only flows whose hostname contains this substring.",
    )] = None,
    method: Annotated[str | None, Field(
        description="Keep only this HTTP method. Case-insensitive, e.g. \"POST\".",
    )] = None,
    status_code: Annotated[int | None, Field(
        description="Keep only flows that returned exactly this status code, e.g. 403.",
    )] = None,
    url_contains: Annotated[str | None, Field(
        description="Keep only flows whose full URL — scheme, host, path and query "
                    "string — contains this substring. Case-insensitive.",
    )] = None,
    content_type: Annotated[str | None, Field(
        description="Keep only flows whose response content-type contains this "
                    "substring, e.g. \"json\".",
    )] = None,
    since_seconds: Annotated[float | None, Field(
        description="Keep only flows captured in the last N seconds. If the user just "
                    "clicked something, 15 isolates exactly what that click triggered.",
    )] = None,
    marked_only: Annotated[bool, Field(
        description="Keep only flows the user has marked in the mitmweb UI — how the "
                    "human points at the flows they care about.",
    )] = False,
    include_assets: Annotated[bool, Field(
        description="Also return static assets (js/css/images/fonts), which are "
                    "filtered out by default.",
    )] = False,
) -> str:
    """List recent flows, newest first — the main way to see what the browser just did.

    Static assets (js/css/images/fonts) are excluded by default: the equivalent of
    mitmproxy's `!~a` filter, and the single biggest signal-to-noise win.

    Filters combine with AND. Each row is a compact summary — id, method, host, path,
    status, content type, size and latency — so follow up with inspect_flow once a row
    looks interesting.
    """
    flows = await mw.flows(fresh=True)
    cutoff = (time.time() - since_seconds) if since_seconds else None
    out = []
    for f in reversed(flows):
        req, resp = f.get("request") or {}, f.get("response") or {}
        if not include_assets and _is_asset(f):
            continue
        if marked_only and not f.get("marked"):
            continue
        if cutoff and (req.get("timestamp_start") or 0) < cutoff:
            continue
        if host and host not in (req.get("pretty_host") or req.get("host") or ""):
            continue
        if method and (req.get("method") or "").upper() != method.upper():
            continue
        if status_code is not None and resp.get("status_code") != status_code:
            continue
        if url_contains and url_contains.lower() not in _url(req).lower():
            continue
        if content_type and content_type.lower() not in _ctype(resp):
            continue
        out.append(_summarize(f))
        if len(out) >= limit:
            break
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool(annotations=_reads("Inspect one flow"))
async def inspect_flow(
    flow_id: FlowId,
    body_max: Annotated[int, Field(
        description="Truncate each body to this many characters. Raise it for a longer "
                    "preview, or use get_content to read one body in full.",
    )] = 4000,
) -> str:
    """Everything about one flow: URL, parsed query parameters, both header sets, both
    bodies, latency, and a ready-to-run curl command that reproduces the request.

    This is the natural second step after list_flows or search_flows has pointed at a
    flow. Reach for get_content instead when one body is long enough to need reading in
    full, and for diff_flows when the question is how two requests differ rather than
    what a single one contains. Binary bodies are reported as a placeholder, and the
    curl command is direct — it does not go back through the proxy.
    """
    f = await _find(flow_id)
    req, resp = f.get("request") or {}, f.get("response") or {}
    req_body, resp_body = await asyncio.gather(
        _body_of(f, "request"), _body_of(f, "response")
    )
    query = dict(parse_qsl(urlsplit(_url(req)).query, keep_blank_values=True))

    return json.dumps(
        {
            "id": f["id"],
            "url": _url(req),
            "query": query or None,
            "latency_ms": _latency_ms(f),
            "marked": f.get("marked") or None,
            "is_replay": f.get("is_replay") or None,
            "request": {
                "method": req.get("method"),
                "http_version": req.get("http_version"),
                "headers": _headers_to_dict(req.get("headers")),
                "body": _clip(req_body, body_max) if req_body else None,
            },
            "response": {
                "status": resp.get("status_code"),
                "reason": resp.get("reason"),
                "headers": _headers_to_dict(resp.get("headers")),
                "body": _clip(resp_body, body_max) if resp_body else None,
            },
            "curl": _curl(req, req_body),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations=_reads("Read one full body"))
async def get_content(
    flow_id: FlowId,
    which: Annotated[Literal["request", "response"], Field(
        description="Which side of the exchange to read: the body the client sent, or "
                    "the one the server returned.",
    )] = "response",
    body_max: Annotated[int, Field(
        description="Truncate the body to this many characters — characters, not bytes, "
                    "so a CJK or emoji-heavy body can be several times this many bytes. "
                    "A truncation notice is appended, so the cap is soft by ~35 chars.",
    )] = 20000,
) -> str:
    """Fetch one body in full, with gzip/brotli already decoded by mitmproxy.

    Use this instead of inspect_flow when a body is large and you need more than
    inspect_flow's 4000-character preview. Binary payloads come back as a short
    placeholder rather than pages of mojibake.
    """
    f = await _find(flow_id)
    text = _decode(await mw.raw(f["id"], which), _ctype(f.get(which) or {}))
    return _clip(text, body_max) if text else "<no body in that direction>"


@mcp.tool(annotations=_reads("Search flows"))
async def search_flows(
    keyword: Annotated[str, Field(
        description="The text to look for. Matching is case-insensitive.",
    )],
    scope: Annotated[Literal["all", "url", "headers", "body"], Field(
        description="Where to look. \"body\" is the slow one because bodies must be "
                    "downloaded; \"url\" and \"headers\" read data already in memory.",
    )] = "all",
    regex: Annotated[bool, Field(
        description="Treat keyword as a Python regular expression, e.g. "
                    "\"sign=[a-f0-9]{32}\", instead of a literal string.",
    )] = False,
    limit: Annotated[int, Field(
        ge=1,  # as in list_flows: the cap is checked after the append
        description="Stop after this many matching flows.",
    )] = 20,
    max_scan: Annotated[int, Field(
        description="How many of the newest flows to examine. Raise it to reach further "
                    "back in the session; the result sets truncated=True when this cap "
                    "was the reason the scan stopped.",
    )] = 200,
    include_assets: Annotated[bool, Field(
        description="Also search static assets, which are skipped by default. Worth "
                    "turning on when hunting for a key hard-coded in a JS bundle.",
    )] = False,
) -> str:
    """Full-text search across flows: "which request carried or returned this value?"

    This is the usual entry point for reverse-engineering an API. Take a distinctive
    value visible in the page (an order number, a username, a token) and search for it
    to find the endpoint that produced it. Scanning runs backwards from the newest flow;
    each hit is a flow summary plus a short snippet around every match, so a URL match
    is distinguishable from a body match at a glance.
    """
    try:
        pat = re.compile(keyword if regex else re.escape(keyword), re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"Invalid regular expression: {e}") from e

    def snippet(text: str) -> str | None:
        m = pat.search(text)
        if not m:
            return None
        return text[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")

    flows = await mw.flows(fresh=True)
    candidates = [f for f in reversed(flows) if include_assets or not _is_asset(f)][:max_scan]

    # Fetch every body we might need up front and concurrently; doing this inline
    # would turn into hundreds of sequential round-trips.
    jobs: list[tuple[str, str]] = []
    if scope in ("all", "body"):
        jobs = [
            (f["id"], which)
            for f in candidates
            for which in ("request", "response")
            if (f.get(which) or {}).get("contentLength")
        ]
    fetched = dict(zip(jobs, await mw.raw_many(jobs), strict=True)) if jobs else {}

    hits = []
    for f in candidates:
        if len(hits) >= limit:
            break
        req, resp = f.get("request") or {}, f.get("response") or {}
        where = []

        if scope in ("all", "url") and (s := snippet(_url(req))):
            where.append({"in": "url", "snippet": s})
        if scope in ("all", "headers"):
            for label, msg in (("request headers", req), ("response headers", resp)):
                blob = "\n".join(f"{k}: {v}" for k, v in (msg.get("headers") or []))
                if s := snippet(blob):
                    where.append({"in": label, "snippet": s})
        if scope in ("all", "body"):
            for which, msg in (("request", req), ("response", resp)):
                text = _decode(fetched.get((f["id"], which)), _ctype(msg))
                if text and not _is_binary_placeholder(text) and (s := snippet(text)):
                    where.append({"in": f"{which} body", "snippet": s})

        if where:
            hits.append({**_summarize(f), "matches": where})

    return json.dumps(
        {
            "keyword": keyword,
            "regex": regex,
            "scanned": len(candidates),
            "hit_count": len(hits),
            "truncated": len(candidates) >= max_scan,
            "hits": hits,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations=_reads("Diff two requests"))
async def diff_flows(
    flow_id_a: FlowId,
    flow_id_b: Annotated[str, Field(
        description="The flow to compare against flow_id_a — usually the same endpoint "
                    "called a second time. Full id or 8-character prefix.",
    )],
    body_max: Annotated[int, Field(
        description="When the bodies cannot be compared field by field (non-JSON), both "
                    "are returned verbatim, truncated to this many characters.",
    )] = 1500,
) -> str:
    """Compare two requests field by field — the tool for reverse-engineering signatures.

    Typical use: call the same endpoint twice (or capture it before and after paging),
    then diff. Parameters that stay the same are omitted from the output, so what
    remains is exactly the set that varies per request: nonce, timestamp, signature.
    That tells you which fields any signing algorithm must reproduce.

    JSON bodies are flattened to a.b.c keys and compared per key. Non-JSON bodies are
    returned verbatim for both sides so you can judge them yourself.
    """
    fa, fb = await asyncio.gather(_find(flow_id_a), _find(flow_id_b))
    ra, rb = fa.get("request") or {}, fb.get("request") or {}
    ba, bb = await asyncio.gather(_body_of(fa, "request"), _body_of(fb, "request"))

    ua, ub = _url(ra), _url(rb)
    qa = dict(parse_qsl(urlsplit(ua).query, keep_blank_values=True))
    qb = dict(parse_qsl(urlsplit(ub).query, keep_blank_values=True))

    result: dict = {
        "a": {"id": fa["id"][:8], "url": ua,
              "status": (fa.get("response") or {}).get("status_code")},
        "b": {"id": fb["id"][:8], "url": ub,
              "status": (fb.get("response") or {}).get("status_code")},
        "same_endpoint": urlsplit(ua).path == urlsplit(ub).path
        and ra.get("method") == rb.get("method"),
    }
    if ra.get("method") != rb.get("method"):
        result["method"] = {"a": ra.get("method"), "b": rb.get("method")}
    if d := _diff_dict(qa, qb):
        result["query_diff"] = d
    if d := _diff_dict(_headers_to_dict(ra.get("headers")), _headers_to_dict(rb.get("headers"))):
        result["header_diff"] = d

    ja, jb = _try_json(ba), _try_json(bb)
    if ja is not None and jb is not None:
        result["body_diff"] = _diff_dict(_flat(ja), _flat(jb)) or "bodies are identical"
    elif ba != bb:
        result["body_a"] = _clip(ba, body_max) if ba else None
        result["body_b"] = _clip(bb, body_max) if bb else None
    else:
        result["body_diff"] = "bodies are identical"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(annotations=_reads("Detect authentication schemes"))
async def detect_auth() -> str:
    """Scan all traffic and report which authentication schemes the site uses.

    Run this when picking up an unfamiliar target: it answers "what exactly do I have
    to forge in order to call this API without a browser?" — a bearer token, a session
    cookie, a custom API-key header, or a signed request. Each finding includes sample
    flow ids you can pass straight to inspect_flow.

    Takes no arguments — it always scans everything currently captured and groups what
    it finds by scheme: bearer/JWT, basic auth, session cookie, API-key header, CSRF
    token, signature header, auth-looking endpoints, and authorization_other for any
    other Authorization scheme — AWS SigV4, Digest and vendor schemes land there.
    Detection is heuristic, based
    on header and path names, so treat an empty result as "nothing obvious" rather than
    as proof the API is open.
    """
    flows = await mw.flows(fresh=True)
    found: dict[str, dict] = {}

    def note(kind: str, detail: str, fid: str) -> None:
        entry = found.setdefault(kind, {"details": set(), "flows": []})
        entry["details"].add(detail)
        if len(entry["flows"]) < 5:
            entry["flows"].append(fid[:8])

    for f in flows:
        req = f.get("request") or {}
        fid = f.get("id") or ""
        path = (req.get("path") or "").split("?")[0].lower()

        for k, v in _headers_to_dict(req.get("headers")).items():
            kl, value = k.lower(), (v or "")
            if kl == "authorization":
                if value.startswith("Bearer "):
                    token = value[7:]
                    # A JWT is header.payload.signature — three base64 segments.
                    note("jwt" if token.count(".") == 2 else "bearer_token",
                         f"{k}: Bearer ...", fid)
                elif value.startswith("Basic "):
                    note("basic_auth", f"{k}: Basic ...", fid)
                else:
                    note("authorization_other", f"{k}: {value[:16]}...", fid)
            elif any(x in kl for x in ("csrf", "xsrf")):
                note("csrf", k, fid)
            elif any(x in kl for x in ("api-key", "apikey", "x-auth-token",
                                       "access-token", "app-key")):
                note("api_key", k, fid)
            elif any(x in kl for x in ("sign", "signature", "-sig")):
                note("signed_request", k, fid)
            elif kl == "cookie":
                for cookie in value.split(";"):
                    name = cookie.strip().split("=")[0]
                    if name and any(s in name.lower() for s in
                                    ("session", "sid", "sess", "token", "auth", "login")):
                        note("session_cookie", name, fid)

        if any(p in path for p in ("/oauth", "/token", "/authorize", "/auth/callback",
                                   "/login", "/signin")):
            note("auth_endpoint", f"{req.get('method')} {path}", fid)

    details = {
        k: {"details": sorted(v["details"])[:12], "sample_flows": v["flows"]}
        for k, v in found.items()
    }
    return json.dumps(
        {
            "scanned": len(flows),
            "detected": sorted(details),
            "details": details,
            "hint": (
                "Pass a sample_flow id to inspect_flow for the full request. If "
                "signed_request appears, use diff_flows on two calls to the same "
                "endpoint to identify which fields the signature covers."
                if details else
                "No common auth signals found — the endpoint may be unauthenticated, "
                "or the credential may live in the request body."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


# Headers the HTTP client manages itself. Copying them verbatim into generated code
# either breaks the request or fights with TLS-fingerprint impersonation.
_SKIP_IN_CODE = {
    "content-length", "host", "connection", "accept-encoding",
    "transfer-encoding", "upgrade-insecure-requests",
}

_CODE_PREAMBLE = {
    "curl_cffi": (
        "from curl_cffi.requests import Session\n\n"
        "# impersonate makes the TLS/JA3 fingerprint match a real browser, which is\n"
        "# usually what gets you past bot detection.\n"
        "# Options: chrome / chrome131 / chrome142 / safari / firefox / edge / ...\n"
        "IMPERSONATE = {imp!r}\n\n\n"
        "def main() -> None:\n"
        "    with Session(impersonate=IMPERSONATE) as s:\n"
    ),
    "httpx": (
        "import httpx\n\n\n"
        "def main() -> None:\n"
        "    with httpx.Client(http2=True, follow_redirects=True, timeout=30) as s:\n"
    ),
    "requests": (
        "import requests\n\n\n"
        "def main() -> None:\n"
        "    with requests.Session() as s:\n"
    ),
}


# Read-only like the rest: it turns captured flows into source text and hands it back.
# Nothing is written to disk and nothing is executed — running the script is the user's
# separate, deliberate act.
@mcp.tool(annotations=_reads("Generate scraper code"))
async def generate_code(
    flow_ids: Annotated[list | str, Field(
        description="The flows to turn into requests, in the order they should run. "
                    "Accepts a list of ids or one comma-separated string; each id may "
                    "be a full id or an 8-character prefix.",
    )],
    framework: Annotated[Literal["curl_cffi", "httpx", "requests", "curl"], Field(
        description="What to emit. curl_cffi fakes a real browser's TLS fingerprint and "
                    "is the best default for scraping; httpx and requests produce plain "
                    "Python; curl produces a bash script instead.",
    )] = "curl_cffi",
    impersonate: Annotated[str, Field(
        description="Browser fingerprint written into curl_cffi output, e.g. chrome, "
                    "chrome131, chrome142, safari, firefox, edge. Ignored by the other "
                    "frameworks.",
    )] = "chrome",
    body_max: Annotated[int, Field(
        description="Truncate each embedded request body to this many characters.",
    )] = 8000,
) -> str:
    """Turn captured flows into a runnable scraper script — the usual final deliverable.

    Requests keep the order you pass them in, and original request headers are kept
    minus the ones the HTTP client manages itself.

    In the three Python outputs they also share one Session, so a "log in, then call the
    API" sequence replays correctly with cookies carried across. framework="curl" cannot
    do that: it emits independent commands with no cookie jar, so each one carries only
    the Cookie header that happened to be captured. Prefer a Python framework whenever
    the sequence depends on a login.

    The script is returned as text: nothing is written to disk and nothing is executed.
    """
    ids = flow_ids if isinstance(flow_ids, list) else flow_ids.split(",")
    ids = [str(x).strip() for x in ids if str(x).strip()]
    if not ids:
        raise ValueError("flow_ids must not be empty")

    flows = [await _find(i) for i in ids]
    bodies = await asyncio.gather(*(_body_of(f, "request") for f in flows))

    if framework == "curl":
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for f, body in zip(flows, bodies, strict=True):
            req = f.get("request") or {}
            lines += [f"# {req.get('method')} {_url(req)}", _curl(req, body), ""]
        return "\n".join(lines)

    out = [
        "#!/usr/bin/env python3",
        '"""Generated by mitmweb-mcp from captured traffic.',
        "",
        'Adapt as needed: add paging loops, concurrency, retries, error handling."""',
        _CODE_PREAMBLE[framework].format(imp=impersonate),
    ]

    for n, (f, body) in enumerate(zip(flows, bodies, strict=True), 1):
        req, resp = f.get("request") or {}, f.get("response") or {}
        method = (req.get("method") or "GET").lower()
        split = urlsplit(_url(req))
        base = f"{split.scheme}://{split.netloc}{split.path}"
        params = dict(parse_qsl(split.query, keep_blank_values=True))
        headers = {k: v for k, v in (req.get("headers") or [])
                   if k.lower() not in _SKIP_IN_CODE}

        out.append(f"        # --- {n}. {req.get('method')} {split.path} "
                   f"(originally returned {resp.get('status_code')}) ---")
        call = [f"        r{n} = s.{method}(", f"            {base!r},"]
        if params:
            call.append(f"            params={params!r},")
        if headers:
            call.append(f"            headers={headers!r},")

        if body and not _is_binary_placeholder(body):
            body = body[:body_max]
            if (parsed := _try_json(body)) is not None:
                call.append(f"            json={parsed!r},")
            elif "x-www-form-urlencoded" in _ctype(req):
                call.append(f"            data={dict(parse_qsl(body, keep_blank_values=True))!r},")
            else:
                call.append(f"            data={body!r},")
        elif body:
            call.append("            # original body was binary — fill this in yourself")

        call.append("        )")
        out += call + [f'        print("{n}.", r{n}.status_code, r{n}.text[:200])', ""]

    out += ["", 'if __name__ == "__main__":', "    main()", ""]
    return "\n".join(out)


@mcp.tool(annotations=ToolAnnotations(
    title="Replay a request",
    # The only tool here that is not a read. It appends to this server's world — a new
    # flow, never an edit to an existing one — but it also puts a real request on the
    # wire, and what it replays is whatever was captured. Replay a DELETE and something
    # gets deleted, so the honest hint is destructive; the append-only guarantee this
    # project makes is about the flow list, not about the target.
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
))
async def replay_flow(
    flow_id: FlowId,
    method: Annotated[str | None, Field(
        description="Override the HTTP method, e.g. send a captured GET as a POST. "
                    "Omit to reuse the original method.",
    )] = None,
    headers: Annotated[dict | str | None, Field(
        description="Headers to add or override, e.g. {\"Authorization\": \"Bearer NEW\"}. "
                    "Merged on top of the original headers; omit to reuse them as-is.",
    )] = None,
    body: Annotated[str | dict | list | None, Field(
        description="Replacement request body; dicts and lists are serialised to JSON. "
                    "Omit to resend the original body.",
    )] = None,
    impersonate: Annotated[str, Field(
        description="Browser TLS fingerprint to present: chrome, chrome131, chrome142, "
                    "safari, firefox, edge and other curl_cffi targets. An unsupported "
                    "value comes back as a structured error, not an exception.",
    )] = "chrome",
    timeout: Annotated[float, Field(
        description="Seconds to wait for the response before giving up.",
    )] = 30.0,
    body_max: Annotated[int, Field(
        description="Truncate the returned response body to this many characters.",
    )] = 4000,
) -> str:
    """Replay a request, optionally rewriting method, headers or body (like Burp Repeater).

    This is the one tool that sends live traffic to the target — use it to check whether
    a token still works, or which parameters an endpoint actually requires.

    The request is sent with curl_cffi using browser TLS fingerprint impersonation, and
    it goes *through your own mitmproxy*, so the result appears as a new flow in your
    mitmweb UI where you can see it. Existing flows are never modified and nothing is
    deleted; redirects are not followed, so every hop stays visible.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return "curl_cffi is not installed, cannot replay. Install it: pip install curl_cffi"

    f = await _find(flow_id)
    req = f.get("request") or {}
    target_method = (method or req.get("method") or "GET").upper()
    target_url = _url(req)

    hdrs = _headers_to_dict(req.get("headers"))
    for drop in ("Host", "host", "Content-Length", "content-length"):
        hdrs.pop(drop, None)
    hdrs.update({str(k): str(v) for k, v in _as_dict(headers, "headers").items()})

    payload = body
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    if payload is None:
        payload = await _body_of(f, "request")
        if _is_binary_placeholder(payload):
            return json.dumps(
                {"ok": False,
                 "error": "Original request body is binary and cannot be replayed "
                          "automatically. Pass an explicit body argument."},
                ensure_ascii=False,
            )

    try:
        async with AsyncSession() as session:
            r = await session.request(
                target_method,
                target_url,
                headers=hdrs,
                content=payload.encode() if isinstance(payload, str) else payload,
                impersonate=impersonate,
                proxy=PROXY,
                verify=False,           # required: traffic goes through mitmproxy's own CA
                timeout=timeout,
                allow_redirects=False,  # keep every hop visible
            )
    except Exception as e:
        return json.dumps(
            {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "hint": f"Check that the proxy at {PROXY} is reachable (mitmweb's "
                        "--listen-port). Set MITMPROXY_PORT if it is not 8080. "
                        "An invalid impersonate value also raises here.",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ok": True,
            "replayed": {
                "method": target_method,
                "url": target_url,
                "impersonate": impersonate,
                "body_sent": _clip(payload, 500) if isinstance(payload, str) else None,
            },
            "status": r.status_code,
            "response_headers": dict(r.headers),
            "response_body": _clip(r.text or "", body_max),
            "note": "This replay is now a new flow in the mitmweb UI; "
                    "list_flows will show it.",
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    """Console-script entry point."""
    mcp.run()


if __name__ == "__main__":
    main()
