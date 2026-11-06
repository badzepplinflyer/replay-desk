"""Replay Desk - gameplay replay buffer, recorder and clip editor.

Run with pythonw.exe (no console). The window is the UI; closing it leaves the
recorder running in the system tray so hotkeys keep working in-game.
"""
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rd import config  # noqa: E402

PORT = 47820
URL = f"http://127.0.0.1:{PORT}/"


def setup_logging():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if sys.stdout:
        root.addHandler(logging.StreamHandler(sys.stdout))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def already_running():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def code_version():
    """Changes whenever the app is updated."""
    from rd.version import __version__
    if getattr(sys, "frozen", False):
        return __version__
    root = Path(__file__).resolve().parent
    files = [root / "app.py", *(root / "rd").glob("*.py")]
    return __version__ + "-" + str(int(max(f.stat().st_mtime for f in files if f.exists())))


def running_version():
    import urllib.request
    try:
        with urllib.request.urlopen(URL + "api/version", timeout=2) as r:
            return r.read().decode().strip()
    except Exception:
        return None          # older builds don't have /api/version


def stop_old_instance():
    """Ask the running copy to quit; if it's too old to understand, end its process."""
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(URL + "api/quit", data=b"{}", method="POST"), timeout=2)
    except Exception:
        pass
    for _ in range(20):
        if not already_running():
            return True
        time.sleep(0.25)
    if config.IS_WINDOWS:
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                 creationflags=config.CREATE_NO_WINDOW).stdout
            pids = {line.split()[-1] for line in out.splitlines()
                    if f"127.0.0.1:{PORT}" in line and "LISTENING" in line}
            for pid in pids:
                if pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True,
                                   creationflags=config.CREATE_NO_WINDOW)
        except Exception:
            pass
    for _ in range(20):
        if not already_running():
            return True
        time.sleep(0.25)
    return False


def find_edge():
    cands = [shutil.which("msedge")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def open_window():
    edge = find_edge() if config.IS_WINDOWS else None
    if edge:
        prof = config.DATA_DIR / "window"
        subprocess.Popen([edge, f"--app={URL}", "--window-size=1440,920",
                          f"--user-data-dir={prof}", "--no-first-run", "--disable-features=Translate"],
                         creationflags=config.CREATE_NO_WINDOW)
    else:
        webbrowser.open(URL)


def play_sound(kind):
    if not config.IS_WINDOWS:
        return
    try:
        import winsound
        tones = {"replay": [(880, 70), (1320, 110)], "rec_start": [(660, 90), (990, 90)],
                 "rec_stop": [(990, 90), (660, 120)], "error": [(300, 250)]}
        for f, d in tones.get(kind, []):
            winsound.Beep(f, d)
    except Exception:
        pass


class HotkeyApplier:
    def __init__(self, manager, recorder, overlay=None):
        self.m, self.rec, self.overlay = manager, recorder, overlay

    def __call__(self):
        s = config.load()
        binds = {s["hotkey_replay"]: self.rec.save_replay,
                 s["hotkey_record"]: self.rec.toggle_recording}
        if self.overlay and self.overlay.available and s.get("hotkey_overlay"):
            binds[s["hotkey_overlay"]] = self.overlay.toggle
        self.m.set_bindings(binds)

    def status_snapshot(self):
        return dict(self.m.status)


def make_tray(recorder, quit_cb, overlay=None):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return None
    try:
        img = Image.open(config.APP_DIR / "web" / "img" / "r_64.png").convert("RGBA")
    except OSError:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 12, 60, 52), 10, fill=(20, 22, 28, 255), outline=(240, 20, 28, 255), width=4)
        d.ellipse((22, 22, 42, 42), fill=(240, 20, 28, 255))
    menu = pystray.Menu(
        pystray.MenuItem("Open Replay Desk", lambda: open_window(), default=True),
        pystray.MenuItem("Save replay", lambda: threading.Thread(target=recorder.save_replay, daemon=True).start()),
        pystray.MenuItem("Start / stop recording",
                         lambda: threading.Thread(target=recorder.toggle_recording, daemon=True).start()),
        pystray.MenuItem("Show / hide overlay", lambda: overlay and overlay.toggle(),
                         visible=bool(overlay and overlay.available)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon: quit_cb(icon)),
    )
    return pystray.Icon("ReplayDesk", img, "Replay Desk", menu)


def set_dpi_aware():
    """Use real pixel coordinates on high-DPI/multi-monitor setups so the overlay
    lands exactly where expected (call before any window is created)."""
    if not config.IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    set_dpi_aware()
    from rd import updater
    if updater.install_pending_on_launch():      # auto-update: install before starting
        return
    if already_running():
        if running_version() == code_version():
            open_window()
            return
        # an older copy from before an update is still running in the tray: replace it
        if not stop_old_instance():
            open_window()
            return
        time.sleep(0.5)
    setup_logging()
    log = logging.getLogger("rd")
    from rd.hotkeys import HotkeyManager
    from rd.recorder import Recorder, clean_buffer_dir
    from rd.server import Events, create_app

    settings = config.load()
    config.ensure_dirs(settings)
    clean_buffer_dir()
    events = Events()
    tray_holder = {}

    def on_event(kind, msg, **kw):
        sound = kw.pop("sound", None)
        events.add(kind, msg, **kw)
        log.info("[%s] %s", kind, msg)
        if config.load()["sound_feedback"]:
            threading.Thread(target=play_sound, args=(sound or ("error" if kind == "error" else ""),),
                             daemon=True).start()
        icon = tray_holder.get("icon")
        if icon and kind in ("saved", "error", "update_available"):
            try:
                icon.notify(msg, "Replay Desk")
            except Exception:
                pass

    recorder = Recorder(on_event)
    hk = HotkeyManager()
    hk.start()
    from rd.overlay import Overlay
    overlay = Overlay(URL, config.load, config.save, open_app=open_window)
    overlay.start()
    applier = HotkeyApplier(hk, recorder, overlay)
    applier()

    quit_holder = {}
    upd = updater.Updater(on_event)
    upd.set_quit(lambda: quit_holder["fn"]())
    app = create_app(recorder, applier, events, code_version(),
                     lambda: threading.Thread(target=quit_holder["fn"], daemon=True).start(), upd, overlay)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Replay Desk running at %s (ffmpeg: %s)", URL, config.FFMPEG)

    if settings["start_buffer_on_launch"]:
        recorder.start()
    upd.start_background()
    if settings.get("overlay_on_launch"):
        overlay.show()
    if "--no-window" not in sys.argv:
        open_window()

    stop = threading.Event()

    def quit_app(icon=None):
        recorder.stop()
        srv.shutdown()
        stop.set()
        if icon:
            icon.stop()

    quit_holder["fn"] = lambda: quit_app(tray_holder.get("icon"))
    icon = make_tray(recorder, quit_app, overlay)
    if icon:
        tray_holder["icon"] = icon
        icon.run()              # blocks until Quit
    else:
        try:
            while not stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            quit_app()
    recorder.stop()


if __name__ == "__main__":
    main()
