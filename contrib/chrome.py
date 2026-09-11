"""mitmweb addon: launch a dedicated Chrome alongside mitmweb, and close it on exit.

One terminal, one command:

    mitmweb -s contrib/chrome.py --set web_password=<TOKEN>

No traffic filtering is applied — you capture everything. Reduce noise at query
time instead (``list_flows(host="target.com")``, or the Filter box in the UI);
that way nothing is hidden before you know what you are looking for.

Differences from mitmproxy's built-in ``browser.start``:
  1. A persistent profile rather than a temp directory, so logins to the target
     site survive across sessions — you do not have to sign in on every run.
  2. Isolated from your everyday browsing: your cookies, extensions and sessions
     never travel through the proxy or end up in a capture. Delete the profile
     directory to reset completely.
  3. Chrome's own background services (sync, component updates, background
     networking, ML model downloads) are switched off. These only affect the
     browser's own requests — nothing the target site issues is touched.

Prerequisite: mitmproxy's CA must be trusted by Chrome. On Linux, Chrome does
not use the mitm.it flow — it reads the shared NSS database, so use certutil:

    certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n mitmproxy \
             -i ~/.mitmproxy/mitmproxy-ca-cert.pem

    # to remove:
    certutil -d sql:$HOME/.pki/nssdb -D -n mitmproxy

Note that the NSS database is per-user, not per-profile: installing the CA makes
every Chrome profile on this account trust mitmproxy-issued certificates.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess

from mitmproxy import ctx

CHROME_BINARIES = (
    "google-chrome-stable", "google-chrome", "chromium",
    "chromium-browser", "chrome", "brave-browser", "microsoft-edge",
)

# Switches for the browser's own background services only. None of these affect
# requests issued by the page under analysis.
# OptimizationHints is Chrome downloading its own ML models — leaving it on adds
# a dozen optimizationguide-pa.googleapis.com/downloads flows to every startup.
QUIET_FLAGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-features=OptimizationHints",
)


class ChromeLauncher:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.profile: str = ""

    def _singleton_pid(self) -> int | None:
        """Return the PID of the Chrome that actually owns the profile.

        Persistent profiles have a trap: if the profile is already in use, a
        newly launched Chrome hands its "open this page" request to the existing
        instance and exits immediately. ``self.proc`` then refers to a zombie,
        and cleaning up by it leaves the real browser running — still pointed at
        whichever proxy port the previous run used.

        SingletonLock is Chrome's own bookkeeping (a symlink to "hostname-PID"),
        which is more reliable here than the process tree or a cmdline match.
        """
        try:
            target = os.readlink(os.path.join(self.profile, "SingletonLock"))
            pid = int(target.rsplit("-", 1)[-1])
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def load(self, loader) -> None:
        loader.add_option(
            "chrome", bool, True,
            "Launch a dedicated Chrome with mitmweb, and close it on exit.",
        )
        loader.add_option(
            "chrome_profile", str, "~/.cache/chrome-mitm",
            "Chrome profile directory, separate from everyday browsing. "
            "Delete it to reset completely.",
        )
        loader.add_option(
            "chrome_url", str, "about:blank",
            "Page Chrome opens on startup.",
        )
        loader.add_option(
            "chrome_quiet", bool, True,
            "Disable Chrome's own background networking, component updates and "
            "account sync. Affects only the browser's own services; no traffic "
            "is filtered and the target site is unaffected.",
        )

    def running(self) -> None:
        if not ctx.options.chrome or self.proc is not None:
            return

        binary = next((b for b in CHROME_BINARIES if shutil.which(b)), None)
        if not binary:
            logging.warning(
                "No Chrome found, skipping launch. Tried: %s", ", ".join(CHROME_BINARIES)
            )
            return

        profile = os.path.expanduser(ctx.options.chrome_profile)
        os.makedirs(profile, exist_ok=True)
        self.profile = profile
        host = ctx.options.listen_host or "127.0.0.1"
        port = ctx.options.listen_port or 8080

        if (existing := self._singleton_pid()) is not None:
            logging.warning(
                "Profile is already held by Chrome (pid %s) — the new window "
                "will be opened by that instance, whose proxy still points at "
                "the previous port. Close it first, or pass "
                "--set chrome_profile=<other directory>.",
                existing,
            )

        cmd = [
            binary,
            f"--user-data-dir={profile}",
            f"--proxy-server=http://{host}:{port}",
            # Chrome bypasses the proxy for localhost by default; without this,
            # traffic to a local service silently never reaches the capture.
            "--proxy-bypass-list=<-loopback>",
            "--no-first-run",
            "--no-default-browser-check",
            *(QUIET_FLAGS if ctx.options.chrome_quiet else ()),
            ctx.options.chrome_url,
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        logging.info("Chrome started (profile=%s, proxy → %s:%s)", profile, host, port)

    def done(self) -> None:
        # Prefer the PID from the lock file: the process we spawned may have
        # handed its window to another instance and exited, and killing only
        # that one would leave an orphaned browser behind.
        pid = self._singleton_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


addons = [ChromeLauncher()]
