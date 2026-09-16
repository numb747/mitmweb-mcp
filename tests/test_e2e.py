#!/usr/bin/env python3
"""End-to-end test: drives the MCP server over a real stdio MCP session.

Launches a real mitmweb on ports 18080/18081, pushes traffic containing known marker
values through it, then calls every tool and asserts on the results.

Requires mitmproxy on PATH and internet access (it talks to httpbingo.org).

    python tests/test_e2e.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PYTHON = os.environ.get("TEST_PYTHON", sys.executable)
MITMWEB = os.environ.get("TEST_MITMWEB", shutil.which("mitmweb") or "mitmweb")
TOKEN = "test-token"
UI = "http://127.0.0.1:18081"
PROXY = "http://127.0.0.1:18080"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


def _compile_err(code: str) -> str:
    """Generated code must survive the real compiler, not merely look plausible."""
    try:
        compile(code, "<generated>", "exec")
        return ""
    except SyntaxError as e:
        return f"{e.msg} (line {e.lineno})"


async def main() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(
        os.environ,
        MITMWEB_URL=UI,
        MITMWEB_TOKEN=TOKEN,
        MITMPROXY_PORT="18080",
        # Works whether or not the package has been pip-installed.
        PYTHONPATH=os.path.join(ROOT, "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    params = StdioServerParameters(
        command=PYTHON, args=["-m", "mitmweb_mcp.server"], env=env, cwd=ROOT
    )

    flows = json.load(urllib.request.urlopen(f"{UI}/flows.json?token={TOKEN}"))
    get_id = next(f["id"] for f in flows if f["request"]["method"] == "GET"
                  and "example.com" in f["request"]["pretty_host"])
    posts = [f["id"] for f in flows if f["request"]["method"] == "POST"]

    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()

        async def call(name: str, **args) -> str:
            return (await session.call_tool(name, args)).content[0].text

        names = [t.name for t in (await session.list_tools()).tools]
        print("\nTools:", names)
        check("exposes 10 tools", len(names) == 10, str(len(names)))
        check("no destructive tool (clear_flows absent)", "clear_flows" not in names)

        print("\n[status]")
        st = json.loads(await call("status"))
        check("connected", st["connected"] is True)
        check("reports the proxy address", st.get("proxy") == PROXY, str(st.get("proxy")))

        print("\n[flow_stats]")
        stats = json.loads(await call("flow_stats"))
        print("   ", json.dumps(stats)[:300])
        check("counts example.com", "example.com" in stats["by_host"])
        check("returns top_endpoints", bool(stats.get("top_endpoints")))
        check("normalises numeric path segments to {n}",
              any("{n}" in k for k in stats["top_endpoints"]),
              str(list(stats["top_endpoints"])[:3]))

        print("\n[list_flows] default noise filtering")
        arr = json.loads(await call("list_flows", limit=10))
        for x in arr:
            print(f"    {x['method']:5} {x['status']} {x['ms']:>5}ms  {x['host']}{x['path'][:40]}")
        check("static assets excluded",
              not any(".css" in x["path"] or ".png" in x["path"] for x in arr))
        check("latency present on every row", all(x.get("ms") is not None for x in arr))
        arr_all = json.loads(await call("list_flows", limit=20, include_assets=True))
        check("include_assets returns more rows", len(arr_all) > len(arr),
              f"{len(arr_all)} > {len(arr)}")

        print("\n[list_flows] time window")
        check("since_seconds=3600 sees traffic",
              len(json.loads(await call("list_flows", since_seconds=3600))) >= 2)
        check("tiny since_seconds filters everything out",
              json.loads(await call("list_flows", since_seconds=0.001)) == [])

        print("\n[list_flows] other filters")
        only_post = json.loads(await call("list_flows", method="POST"))
        check("method=POST returns only POSTs",
              bool(only_post) and all(x["method"] == "POST" for x in only_post))
        check("content_type=json matches",
              len(json.loads(await call("list_flows", content_type="json"))) >= 1)
        check("marked_only is empty when nothing is marked",
              json.loads(await call("list_flows", marked_only=True)) == [])

        print("\n[inspect_flow]")
        det = json.loads(await call("inspect_flow", flow_id=get_id, body_max=120))
        check("captures the Authorization header",
              det["request"]["headers"].get("Authorization") == "Bearer SECRET123")
        check("curl is direct, not proxied", " -x " not in det["curl"])
        check("latency_ms is an int", isinstance(det.get("latency_ms"), int))
        qd = json.loads(await call("inspect_flow", flow_id=posts[0]))
        check("query string parsed out", qd.get("query", {}).get("page") == "1",
              str(qd.get("query")))

        print("\n[binary handling]")
        png = next((f["id"] for f in flows if "image/png" in
                    dict(f.get("response", {}).get("headers") or []).get("content-type", "")),
                   None)
        pb = await call("get_content", flow_id=png[:8], which="response") if png else ""
        check("PNG body reported as binary, not mojibake", pb.startswith("<binary"), pb[:60])

        print("\n[get_content]")
        body = await call("get_content", flow_id=get_id[:8], which="response", body_max=80)
        check("short id prefix resolves", "Example Domain" in body, repr(body[:45]))

        print("\n[search_flows]")
        res = json.loads(await call("search_flows", keyword="MAGIC-VALUE-42"))
        check("finds MAGIC-VALUE-42", res["hit_count"] >= 1, f"hits={res['hit_count']}")
        check("match located in a body",
              any("body" in m["in"] for m in res["hits"][0]["matches"]) if res["hits"] else False)
        check("scope=headers finds the token in request headers",
              json.loads(await call("search_flows", keyword="SECRET123",
                                    scope="headers"))["hit_count"] >= 1)
        check("no match returns cleanly",
              json.loads(await call("search_flows",
                                    keyword="no-such-value-zzz"))["hit_count"] == 0)
        check("regex mode matches",
              json.loads(await call("search_flows", keyword=r"MAGIC-VALUE-\d+",
                                    regex=True))["hit_count"] >= 1)
        bad = await call("search_flows", keyword="[unclosed", regex=True)
        check("invalid regex reports clearly", "Invalid regular expression" in bad, bad[:60])

        print("\n[diff_flows]")
        df = json.loads(await call("diff_flows", flow_id_a=posts[0], flow_id_b=posts[1]))
        print("   ", json.dumps(df)[:420])
        check("recognises the same endpoint", df.get("same_endpoint") is True)
        check("query diff surfaces the changed nonce",
              "nonce" in json.dumps(df.get("query_diff", {})))
        check("unchanged query param is omitted",
              "page" not in json.dumps(df.get("query_diff", {}).get("changed", {})))
        check("body diff surfaces sign", "sign" in json.dumps(df.get("body_diff", {})))
        check("body diff surfaces the nested meta.ts",
              "meta.ts" in json.dumps(df.get("body_diff", {})))
        check("unchanged nested field meta.ver is omitted",
              "meta.ver" not in json.dumps(df.get("body_diff", {})))

        print("\n[detect_auth]")
        au = json.loads(await call("detect_auth"))
        print("   ", json.dumps(au["detected"]))
        check("detects bearer_token", "bearer_token" in au["detected"], str(au["detected"]))
        check("provides sample_flows",
              bool(au["details"].get("bearer_token", {}).get("sample_flows")))

        print("\n[generate_code] curl_cffi")
        code = await call("generate_code", flow_ids=[get_id[:8], posts[0][:8]])
        print("   ", code[:230].replace("\n", "\n    "))
        check("emits curl_cffi code", "from curl_cffi.requests import Session" in code)
        check("includes impersonation", "IMPERSONATE" in code)
        check("keeps the Authorization header", "Bearer SECRET123" in code)
        check("drops Content-Length", "Content-Length" not in code)
        check("query string becomes params=", "params={" in code and "'page': '1'" in code)
        check("JSON body becomes json=", "json={" in code)
        check("generated code compiles", not _compile_err(code), _compile_err(code))

        print("\n[generate_code] other frameworks")
        for fw, marker in (("httpx", "import httpx"), ("requests", "import requests")):
            c = await call("generate_code", flow_ids=get_id[:8], framework=fw)
            check(f"{fw} output compiles and imports correctly",
                  marker in c and not _compile_err(c), _compile_err(c))
        sh = await call("generate_code", flow_ids=f"{get_id[:8]},{posts[0][:8]}",
                        framework="curl")
        check("comma-separated ids produce two curl commands",
              sh.count("curl -X") == 2, str(sh.count("curl -X")))
        check("invalid framework rejected",
              "framework" in await call("generate_code", flow_ids=get_id[:8], framework="php"))

        print("\n[replay_flow] verbatim")
        before = len(json.loads(await call("list_flows", limit=99, include_assets=True)))
        rp = json.loads(await call("replay_flow", flow_id=get_id))
        check("replay succeeded", rp.get("ok") is True, str(rp.get("error", ""))[:120])
        check("replay returned 200", rp.get("status") == 200, str(rp.get("status")))
        await asyncio.sleep(2.5)
        after = len(json.loads(await call("list_flows", limit=99, include_assets=True)))
        check("replay appears in the shared flow list", after > before, f"{before} -> {after}")

        print("\n[replay_flow] modified")
        raw = await call("replay_flow", flow_id=posts[0],
                         body='{"user":"alice","trace":"REPLAYED-99"}',
                         headers={"X-Injected": "yes"}, impersonate="chrome131")
        if not raw.lstrip().startswith("{"):
            print("    raw:", raw[:400])
            check("modified replay succeeded", False, raw[:110])
            rp2 = {}
        else:
            rp2 = json.loads(raw)
            check("modified replay succeeded", rp2.get("ok") is True,
                  str(rp2.get("error", ""))[:150])
        check("server echoed the rewritten body", "REPLAYED-99" in (rp2.get("response_body") or ""))
        check("server echoed the injected header", "X-Injected" in (rp2.get("response_body") or ""))
        check("impersonation argument took effect",
              rp2.get("replayed", {}).get("impersonate") == "chrome131")

        print("\n[error handling]")
        check("unknown flow id reports clearly",
              "No flow matching" in await call("inspect_flow", flow_id="deadbeef"))
        check("invalid 'which' rejected",
              "which" in await call("get_content", flow_id=get_id[:8], which="bogus"))
        check("invalid impersonate returns a structured error",
              json.loads(await call("replay_flow", flow_id=get_id,
                                    impersonate="netscape1")).get("ok") is False)


if __name__ == "__main__":
    proc = subprocess.Popen(
        [MITMWEB, "--listen-port", "18080", "--set", "web_host=127.0.0.1",
         "--set", "web_port=18081", "--set", "web_open_browser=false",
         "--set", f"web_password={TOKEN}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(7)
        import httpx

        with httpx.Client(proxy=PROXY, verify=False, timeout=20) as c:
            # A request carrying a bearer token.
            c.get("https://example.com", headers={"Authorization": "Bearer SECRET123"})
            # The same endpoint twice, differing only in nonce/sign/meta.ts — this is
            # what diff_flows must isolate, while leaving page and meta.ver alone.
            for nonce, sign, ts in (("aaa", "1111", 1000), ("bbb", "2222", 2000)):
                c.post(f"https://httpbingo.org/post?page=1&nonce={nonce}",
                       json={"user": "bob", "trace": "MAGIC-VALUE-42",
                             "sign": sign, "meta": {"ts": ts, "ver": "2"}})
            # A numeric path segment, for top_endpoints normalisation.
            c.get("https://httpbingo.org/status/204")
            # Static assets, for default noise filtering and binary detection.
            c.get("https://httpbingo.org/encoding/utf8", headers={"Accept": "text/css"})
            c.get("https://httpbingo.org/image/png")
        time.sleep(2)
        asyncio.run(main())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}")
    sys.exit(1 if failures else 0)
