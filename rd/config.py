"""Settings, paths and FFmpeg discovery for Replay Desk."""
import json
import os
import shutil
import sys
import threading
from pathlib import Path

APP_NAME = "Replay Desk"
if getattr(sys, "frozen", False):
    # Installed build (PyInstaller): web files and ffmpeg sit in the bundle folder
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    APP_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ReplayDesk"
    DEFAULT_OUTPUT = Path.home() / "Videos" / "Replay Desk"
else:  # dev / testing on other systems
    DATA_DIR = Path.home() / ".replaydesk"
    DEFAULT_OUTPUT = Path.home() / "ReplayDesk-Videos"

BUFFER_DIR = DATA_DIR / "buffer"
CACHE_DIR = DATA_DIR / "cache"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "replaydesk.log"

DEFAULTS = {
    "output_dir": str(DEFAULT_OUTPUT),
    "monitor": 0,                 # 0 = primary display
    "fps": 60,
    "quality": "high",            # high / medium / low  -> NVENC CQ 19 / 23 / 27
    "encoder": "nvenc",           # nvenc / amf / x264
    "buffer_seconds": 300,        # replay buffer length
    "game_audio": True,
    "game_device": "",            # "" = Windows default output
    "mic_enabled": True,
    "mic_device": "",             # "" = Windows default mic
    "mic_volume": 1.0,
    "audio_offset_ms": 0,         # + delays audio, - advances it
    "hotkey_replay": "Ctrl+Alt+F10",
    "hotkey_record": "Ctrl+Alt+F9",
    "hotkey_overlay": "Ctrl+Alt+O",
    "overlay_on_launch": False,
    "overlay_style": "side",      # side = slides in from the screen edge, float = small draggable panel
    "overlay_x": -1,
    "overlay_y": -1,
    "sound_feedback": True,
    "start_buffer_on_launch": True,
    "name_by_game": True,
    "auto_update": False,         # True = download quietly, install on next launch
}

QUALITY_CQ = {"high": 19, "medium": 23, "low": 27}

_lock = threading.Lock()


def ensure_dirs(settings=None):
    for d in (DATA_DIR, BUFFER_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    s = settings or load()
    out = Path(s["output_dir"])
    for sub in ("Replays", "Recordings", "Clips", "Compilations"):
        (out / sub).mkdir(parents=True, exist_ok=True)


def load():
    with _lock:
        data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                pass
        return data


def save(new):
    with _lock:
        data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                pass
        for k, v in new.items():
            if k in DEFAULTS:
                data[k] = type(DEFAULTS[k])(v) if DEFAULTS[k] is not None else v
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), "utf-8")
        return data


def _find_tool(name):
    exe = name + (".exe" if IS_WINDOWS else "")
    # 1. bundled copy downloaded by setup into ./ffmpeg/
    bundled = APP_DIR / "ffmpeg"
    if bundled.exists():
        for p in bundled.rglob(exe):
            return str(p)
    # 2. PATH
    p = shutil.which(name)
    if p:
        return p
    # 3. winget links folder (PATH may not be refreshed yet)
    if IS_WINDOWS:
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / exe
        if links.exists():
            return str(links)
        pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        if pkgs.exists():
            for p in pkgs.glob(f"Gyan.FFmpeg*/**/bin/{exe}"):
                return str(p)
    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")

# Hide console windows for child processes on Windows
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
