#!/usr/bin/env python3
"""Check the Chrome addon's proxy-bypass rules. Needs mitmproxy; launches no browser.

Two things are easy to get wrong here and neither shows up as an error at runtime —
Chrome just silently proxies the UI again, and the capture starts feeding itself:

  * a bypass rule matches the host as written in the URL (proxy resolution happens
    before name resolution), so 127.0.0.1:8081 does not cover http://localhost:8081/;
  * web_port/web_host belong to mitmweb's WebAddon, so reading them under mitmdump
    raises inside the running() hook and Chrome never launches at all.

    /tmp/mitmproxy-venv/bin/python tests/check_chrome_bypass.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contrib"))

import chrome  # noqa: E402
from mitmproxy.test import taddons  # noqa: E402

LOOPBACK = "<-loopback>;localhost:8081;127.0.0.1:8081;[::1]:8081"

CASES = [
    # (label, web_host, web_port, expected)
    ("default mitmweb bind", "127.0.0.1", 8081, LOOPBACK),
    ("web_host=localhost", "localhost", 8081, LOOPBACK),
    ("IPv6 loopback bind", "::1", 8081, LOOPBACK),
    ("wildcard bind", "0.0.0.0", 8081, LOOPBACK),
    ("IPv6 wildcard bind", "::", 8081, LOOPBACK),
    ("LAN bind", "192.168.1.5", 8081, f"{LOOPBACK};192.168.1.5:8081"),
    # Bracketed, or Chrome has no valid rule to match.
    ("IPv6 non-loopback bind", "fe80::1", 8081, f"{LOOPBACK};[fe80::1]:8081"),
    ("custom UI port", "127.0.0.1", 9090,
     "<-loopback>;localhost:9090;127.0.0.1:9090;[::1]:9090"),
    # No web UI to keep out of the capture, and no port to guess at.
    ("not mitmweb", None, None, "<-loopback>"),
]


def check_rules() -> list[str]:
    errors = []
    for label, host, port, expected in CASES:
        got = chrome._bypass_list(host, port)
        status = "ok  " if got == expected else "FAIL"
        print(f"  {status} {label:24} {got}")
        if got != expected:
            errors.append(f"{label}: expected {expected!r}, got {got!r}")
    return errors


def check_runs_without_web_options() -> list[str]:
    """Drive running() against core options only — i.e. what mitmdump provides."""
    captured: dict[str, list[str]] = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    real_popen, real_which = chrome.subprocess.Popen, chrome.shutil.which
    chrome.subprocess.Popen = FakePopen
    chrome.shutil.which = lambda b: "/bin/true" if b == chrome.CHROME_BINARIES[0] else None
    try:
        addon = chrome.ChromeLauncher()
        with taddons.context(addon) as tctx:
            if hasattr(tctx.options, "web_port"):
                return ["core options unexpectedly carry web_port; this check is vacuous"]
            tctx.options.update(
                chrome_capture_localhost=True,
                chrome_profile="/tmp/chrome-bypass-check",
            )
            try:
                addon.running()
            except Exception as e:
                return [f"running() raised under core-only options: {type(e).__name__}: {e}"]
    finally:
        chrome.subprocess.Popen, chrome.shutil.which = real_popen, real_which

    rules = [a for a in captured.get("cmd", []) if a.startswith("--proxy-bypass-list=")]
    print(f"  ok   {'no web options (mitmdump)':24} {rules[0] if rules else '<none>'}")
    if rules != ["--proxy-bypass-list=<-loopback>"]:
        return [f"expected a bare <-loopback> rule without a UI port, got {rules}"]
    return []


def main() -> int:
    print("bypass rules:")
    errors = check_rules() + check_runs_without_web_options()
    for e in errors:
        print(f"FAIL  {e}", file=sys.stderr)
    print(f"{len(errors)} failure(s)" if errors else "chrome bypass rules OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
