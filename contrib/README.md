# contrib — companion tooling

Optional helpers for the capture side of the workflow. The MCP server does not
depend on any of this; use it if it fits how you work.

| File | What it is |
|---|---|
| `chrome.py` | mitmweb addon that launches a dedicated Chrome pointed at the proxy and closes it on exit |
| `mitm-start` | start mitmweb in the background with the addon, with logging and a duplicate-start guard |
| `mitm-stop` | stop mitmweb and clean up Chrome, including orphans |

## Setup

**1. Trust mitmproxy's CA in Chrome.** On Linux, Chrome does not use the
`mitm.it` flow that Firefox does — it reads the shared NSS database, so the
certificate has to go in with `certutil` (part of the `nss` package):

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n mitmproxy \
         -i ~/.mitmproxy/mitmproxy-ca-cert.pem
```

To verify, and to remove it later:

```bash
certutil -d sql:$HOME/.pki/nssdb -L | grep mitmproxy
certutil -d sql:$HOME/.pki/nssdb -D -n mitmproxy
```

The NSS database is per-user, not per-profile: this makes every Chrome profile
on the account trust mitmproxy-issued certificates. Since the CA's private key
sits in `~/.mitmproxy/` on the same machine, that is the usual trade-off — but
it is worth making the decision knowingly.

**2. Write the shared config.** Both scripts read it, and the token must match
the one in your MCP client configuration:

```bash
mkdir -p ~/.config/mitmweb-mcp
cat > ~/.config/mitmweb-mcp/env <<EOF
MITMWEB_TOKEN=$(openssl rand -hex 8)
MITMPROXY_PORT=8080
MITMWEB_PORT=8081
CHROME_MITM_PROFILE=\$HOME/.cache/chrome-mitm
EOF
chmod 600 ~/.config/mitmweb-mcp/env
```

**3. Put the commands on your PATH** (optional):

```bash
ln -s "$PWD/contrib/mitm-start" ~/.local/bin/mitm-start
ln -s "$PWD/contrib/mitm-stop"  ~/.local/bin/mitm-stop
```

## Use

```bash
mitm-start                      # background, logs to a file
mitm-start https://target.com   # ...and open a page straight away
mitm-start -f                   # foreground, Ctrl-C to stop
mitm-start -l                   # follow the log

mitm-stop -s                    # status only
mitm-stop                       # stop mitmweb and Chrome
```

Or run mitmweb yourself and just use the addon:

```bash
mitmweb -s contrib/chrome.py --set web_password=<TOKEN>
```

## Notes

**No traffic filtering is applied.** You capture everything; reduce noise at
query time with `list_flows(host="target.com")` or the Filter box in the UI.
Filtering globally means deciding what to discard *before* you know what you are
looking for, and a mistake there is silent. Note also that mitmweb's
`view_filter` applies to `/flows.json` as well, so an over-broad filter hides
data from the MCP server too, not just the UI. (It is only a view, though —
clearing the filter brings everything back.)

What the addon *does* switch off is Chrome's own background services: sync,
component updates, background networking, and ML model downloads. Those are the
browser's own requests, not the target site's — leaving `OptimizationHints` on
alone adds a dozen `optimizationguide-pa.googleapis.com` flows per startup.
Pass `--set chrome_quiet=false` for a completely stock Chrome.

**A dedicated profile is not just tidiness.** If Chrome is already running,
`--proxy-server` on a new invocation is *silently ignored* — the flag goes to a
process that already exists, so the browser opens, pages load, and nothing
appears in mitmweb. `--user-data-dir` forces a separate process and sidesteps
that entirely. It also keeps your everyday cookies and sessions out of the
capture, which matters as soon as you share a capture file or generate code from
one.

**Persistent profiles have a trap, which these scripts handle.** Launch a second
Chrome against a profile that is already in use and it hands its window to the
existing instance and exits immediately — so the addon's handle refers to a
zombie, and the real browser survives with its proxy still pointed at the
previous port. Both `chrome.py` and `mitm-stop` therefore locate Chrome through
the profile's `SingletonLock` symlink rather than the process tree.
