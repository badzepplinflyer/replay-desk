"""Checks GitHub Releases for a newer Replay Desk and installs it.

Flow: check latest release -> if newer, show a banner (or, with auto-update on,
download quietly and install on next launch) -> download ReplayDesk-Setup.exe ->
run it silently -> the installer replaces the app and reopens it.
Settings and videos live outside the install folder, so they are kept.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config
from .version import INSTALLER_ASSET, REPO, __version__

log = logging.getLogger("rd.updater")

UPDATES_DIR = config.DATA_DIR / "updates"
PENDING_FILE = UPDATES_DIR / "pending.json"
CHECK_EVERY = 6 * 3600
FROZEN = getattr(sys, "frozen", False)   # True when running the installed .exe


def parse_ver(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(remote, local=__version__):
    return parse_ver(remote) > parse_ver(local)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": f"ReplayDesk/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


class Updater:
    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda *a, **k: None)
        self.state = {
            "current": __version__,
            "installed_build": FROZEN,
            "available": False,
            "version": None,
            "notes": "",
            "published": None,
            "checking": False,
            "last_check": None,
            "error": None,
            "downloading": False,
            "progress": 0.0,
            "ready": False,           # installer downloaded and verified
        }
        self._asset = None
        self._lock = threading.Lock()
        self._quit = None

    def set_quit(self, fn):
        self._quit = fn

    def snapshot(self):
        return dict(self.state)

    # ------------------------------------------------------------------ check
    def start_background(self):
        def loop():
            time.sleep(8)               # let the app finish starting
            while True:
                self.check()
                time.sleep(CHECK_EVERY)
        threading.Thread(target=loop, daemon=True).start()

    def check(self):
        if self.state["checking"]:
            return self.snapshot()
        self.state.update(checking=True, error=None)
        try:
            with _get(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
                rel = json.loads(r.read())
            tag = rel.get("tag_name", "")
            asset = next((a for a in rel.get("assets", []) if a.get("name") == INSTALLER_ASSET), None)
            newer = is_newer(tag) and asset is not None
            self.state.update(available=newer, version=tag.lstrip("vV") if newer else None,
                              notes=(rel.get("body") or "").strip() if newer else "",
                              published=rel.get("published_at"))
            self._asset = asset if newer else None
            if newer:
                log.info("update available: %s", tag)
                if self.state.get("_announced") != tag:
                    self.state["_announced"] = tag
                    self.on_event("update_available", f"Replay Desk {tag.lstrip('vV')} is available")
                self.state["ready"] = self._installer_ready(tag.lstrip("vV"), asset)
                if config.load().get("auto_update") and FROZEN and not self.state["ready"]:
                    threading.Thread(target=self.download, kwargs={"then_install": False}, daemon=True).start()
        except Exception as e:
            # no internet, repo not published yet, GitHub rate limit... try again later
            self.state["error"] = str(e)
            log.info("update check failed: %s", e)
        finally:
            self.state.update(checking=False, last_check=time.time())
        return self.snapshot()

    # --------------------------------------------------------------- download
    def _installer_path(self, version):
        return UPDATES_DIR / f"ReplayDesk-Setup-{version}.exe"

    def _installer_ready(self, version, asset):
        p = self._installer_path(version)
        return p.exists() and asset and p.stat().st_size == asset.get("size")

    def download(self, then_install=False):
        if not self._asset or not self.state["version"]:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        version = self.state["version"]
        target = self._installer_path(version)
        try:
            if not self._installer_ready(version, self._asset):
                UPDATES_DIR.mkdir(parents=True, exist_ok=True)
                for old in UPDATES_DIR.glob("ReplayDesk-Setup-*.exe"):
                    old.unlink(missing_ok=True)
                self.state.update(downloading=True, progress=0.0, error=None)
                tmp = target.with_suffix(".part")
                total = self._asset.get("size") or 0
                done = 0
                with _get(self._asset["browser_download_url"], timeout=60) as r, open(tmp, "wb") as fh:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            self.state["progress"] = done / total
                if total and done != total:
                    tmp.unlink(missing_ok=True)
                    raise IOError("Download was incomplete")
                os.replace(tmp, target)
            PENDING_FILE.write_text(json.dumps({"version": version, "path": str(target)}), "utf-8")
            self.state.update(downloading=False, progress=1.0, ready=True)
            log.info("update %s downloaded", version)
            if then_install:
                self.install()
            return True
        except Exception as e:
            self.state.update(downloading=False, error=f"Download failed: {e}")
            log.exception("update download failed")
            return False
        finally:
            self._lock.release()

    # ---------------------------------------------------------------- install
    def install(self):
        version = self.state["version"]
        path = self._installer_path(version) if version else None
        if not path or not path.exists():
            return False
        if not FROZEN:
            self.state["error"] = "Updates install only in the installed version of Replay Desk"
            return False
        run_installer(path)
        self.on_event("update", f"Installing Replay Desk {version}…")
        if self._quit:
            threading.Timer(0.8, self._quit).start()
        return True


def run_installer(path):
    flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)
    try:
        PENDING_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def install_pending_on_launch():
    """Auto-update mode: if a newer installer was downloaded earlier, run it now
    (before the app starts). Returns True if the app should exit."""
    if not FROZEN or not PENDING_FILE.exists():
        return False
    try:
        info = json.loads(PENDING_FILE.read_text("utf-8"))
        path = Path(info["path"])
        if not config.load().get("auto_update"):
            return False
        if not path.exists() or not is_newer(info["version"]):
            PENDING_FILE.unlink(missing_ok=True)
            return False
        log.info("installing pending update %s", info["version"])
        run_installer(path)
        return True
    except Exception:
        log.exception("pending update failed")
        try:
            PENDING_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return False
