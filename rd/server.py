"""Local web server for the Replay Desk UI (127.0.0.1 only)."""
import ctypes
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, make_response, request, send_file, send_from_directory

from . import audio, config, media

log = logging.getLogger("rd.server")

FOLDERS = ("Replays", "Recordings", "Clips", "Compilations")
CAPTURE_KEYS = {"monitor", "fps", "quality", "encoder", "game_audio", "game_device", "mic_enabled",
                "mic_device", "mic_volume", "audio_offset_ms", "output_dir"}


class Events:
    def __init__(self):
        self.items, self.next_id, self.lock = [], 1, threading.Lock()

    def add(self, kind, msg, **extra):
        with self.lock:
            self.items.append({"id": self.next_id, "kind": kind, "msg": msg, "t": time.time(), **extra})
            self.next_id += 1
            self.items = self.items[-100:]

    def since(self, i):
        with self.lock:
            return [e for e in self.items if e["id"] > i]


def list_monitors():
    mons = []
    if not hasattr(ctypes, "windll"):
        return [{"index": 0, "label": "Display 1"}]
    from ctypes import wintypes

    class MONITORINFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    def cb(hmon, hdc, rect, data):
        mi = MONITORINFOEX()
        mi.cbSize = ctypes.sizeof(MONITORINFOEX)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        mons.append({"w": r.right - r.left, "h": r.bottom - r.top, "primary": bool(mi.dwFlags & 1)})
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        ctypes.windll.user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    except Exception:
        pass
    # ddagrab output 0 is normally the primary display
    mons.sort(key=lambda m: not m["primary"])
    return [{"index": i, "label": f"Display {i + 1} - {m['w']}x{m['h']}" + (" (primary)" if m["primary"] else "")}
            for i, m in enumerate(mons)] or [{"index": 0, "label": "Display 1"}]


def create_app(recorder, hotkeys_apply, events, version="dev", quit_cb=None, updater=None):
    static = config.APP_DIR / "web"
    app = Flask(__name__, static_folder=None)
    app.logger.setLevel(logging.WARNING)
    probe_cache, jobs = {}, {}

    def out_root():
        return Path(config.load()["output_dir"])

    def resolve(rel):
        """'Replays/name.mp4' -> absolute path inside the output folder."""
        if not rel or "/" not in rel:
            abort(400)
        folder, name = rel.split("/", 1)
        if folder not in FOLDERS or "/" in name or "\\" in name or name.startswith("."):
            abort(400)
        p = out_root() / folder / name
        if not p.exists():
            abort(make_response(jsonify(ok=False, error=f"{name} is no longer in the {folder} folder"), 404))
        return p

    def info_for(p):
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime)
        if key not in probe_cache:
            try:
                probe_cache[key] = media.probe(p)
            except Exception as e:
                probe_cache[key] = {"duration": 0, "error": str(e), "audio_tracks": []}
        return probe_cache[key]

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(static, "index.html")

    @app.get("/web/<path:name>")
    def web(name):
        resp = send_from_directory(static, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # --------------------------------------------------------------- status
    @app.get("/api/version")
    def version_info():
        return version

    @app.post("/api/quit")
    def quit_app():
        if request.remote_addr != "127.0.0.1" or not quit_cb:
            abort(403)
        quit_cb()
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        since = int(request.args.get("since", 0))
        st = recorder.status()
        st["events"] = events.since(since)
        st["hotkeys"] = hotkeys_apply.status_snapshot()
        st["update"] = updater.snapshot() if updater else None
        return jsonify(st)

    @app.post("/api/update/check")
    def update_check():
        return jsonify(updater.check() if updater else {})

    @app.post("/api/update/install")
    def update_install():
        if not updater or not updater.state["available"]:
            return jsonify(ok=False, error="No update available"), 400
        if recorder.recording_started_at:
            return jsonify(ok=False, error="Stop your recording first, then update"), 409
        if not updater.state["installed_build"]:
            return jsonify(ok=False, error="This copy runs from source; updates install only in the installed version"), 400
        threading.Thread(target=updater.download, kwargs={"then_install": True}, daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/buffer/<action>")
    def buffer(action):
        if action == "start":
            recorder.start()
        elif action == "stop":
            recorder.stop()
            events.add("buffer", "Replay buffer stopped")
        return jsonify(ok=True)

    @app.post("/api/replay/save")
    def save_replay():
        threading.Thread(target=recorder.save_replay, daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/record/toggle")
    def record_toggle():
        threading.Thread(target=recorder.toggle_recording, daemon=True).start()
        return jsonify(ok=True)

    # ------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return jsonify(config.load())

    @app.post("/api/settings")
    def set_settings():
        new = request.get_json(force=True) or {}
        old = config.load()
        saved = config.save(new)
        config.ensure_dirs(saved)
        if any(old.get(k) != saved.get(k) for k in ("hotkey_replay", "hotkey_record")):
            hotkeys_apply()
        capture_changed = any(old.get(k) != saved.get(k) for k in CAPTURE_KEYS)
        recorder.settings = saved
        if capture_changed and recorder.want_running:
            threading.Thread(target=recorder.restart, daemon=True).start()
        return jsonify(saved)

    @app.get("/api/devices")
    def devices():
        d = audio.list_devices()
        d["monitors"] = list_monitors()
        return jsonify(d)

    # -------------------------------------------------------------- library
    @app.get("/api/library")
    def library():
        root = out_root()
        items = []
        for folder in FOLDERS:
            d = root / folder
            if not d.exists():
                continue
            for p in d.glob("*.mp4"):
                if p.name.endswith(".part.mp4"):
                    continue
                st = p.stat()
                inf = info_for(p)
                items.append({"id": f"{folder}/{p.name}", "folder": folder, "name": p.stem,
                              "size": st.st_size, "mtime": st.st_mtime,
                              "duration": inf.get("duration", 0),
                              "width": inf.get("width"), "height": inf.get("height"),
                              "has_thumb": media.thumb_path(p).exists()})
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return jsonify(items=items, root=str(root))

    @app.get("/media/<folder>/<path:name>")
    def media_file(folder, name):
        p = resolve(f"{folder}/{name}")
        return send_file(p, mimetype="video/mp4", conditional=True)

    @app.get("/thumb/<folder>/<path:name>")
    def thumb(folder, name):
        p = resolve(f"{folder}/{name}")
        t = media.thumb_path(p)
        if not t.exists():
            media.make_thumbnail(p)
        if not t.exists():
            abort(404)
        return send_file(t, mimetype="image/jpeg", max_age=3600)

    @app.get("/api/info")
    def info():
        p = resolve(request.args.get("f"))
        return jsonify(info_for(p))

    @app.get("/api/filmstrip")
    def filmstrip():
        p = resolve(request.args.get("f"))
        count = max(8, min(120, int(request.args.get("count", 40))))
        try:
            img = media.filmstrip(p, count=count)
        except Exception as e:
            log.warning("filmstrip failed: %s", e)
            abort(500)
        return send_file(img, mimetype="image/jpeg", max_age=86400)

    @app.post("/api/file/delete")
    def delete_file():
        p = resolve((request.get_json(force=True) or {}).get("f"))
        try:
            from send2trash import send2trash  # recycle bin if available
            send2trash(str(p))
        except ImportError:
            p.unlink()
        media.thumb_path(p).unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.post("/api/file/rename")
    def rename_file():
        body = request.get_json(force=True) or {}
        p = resolve(body.get("f"))
        new = "".join(c for c in (body.get("name") or "").strip() if c not in '<>:"/\\|?*').strip(". ")
        if not new:
            abort(400)
        target = p.with_name(new + ".mp4")
        if target.exists():
            return jsonify(ok=False, error="A file with that name already exists"), 409
        p.rename(target)
        t = media.thumb_path(p)
        if t.exists():
            t.rename(media.thumb_path(target))
        return jsonify(ok=True, id=f"{p.parent.name}/{target.name}")

    @app.post("/api/file/reveal")
    def reveal():
        body = request.get_json(force=True) or {}
        if body.get("f"):
            p = resolve(body["f"])
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            if os.name == "nt":
                os.startfile(str(out_root()))
        return jsonify(ok=True)

    # --------------------------------------------------------------- export
    @app.post("/api/export")
    def export():
        b = request.get_json(force=True) or {}
        src = resolve(b.get("f"))
        inf = info_for(src)
        dur = inf.get("duration", 0)
        segs = []
        for a, e in b.get("segments") or []:
            a, e = max(0.0, float(a)), min(float(e), dur or float(e))
            if e - a >= 0.05:
                segs.append((a, e))
        if not segs:
            return jsonify(ok=False, error="Add at least one segment"), 400
        name = "".join(c for c in (b.get("name") or "").strip() if c not in '<>:"/\\|?*').strip(". ")
        name = name or f"{src.stem}_clip"
        out_dir = out_root() / "Clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.mp4"
        i = 2
        while out.exists():
            out = out_dir / f"{name} ({i}).mp4"
            i += 1
        s = config.load()
        encoder = b.get("encoder") or s["encoder"]
        quality = b.get("quality") or "high"
        res = b.get("resolution") or None
        vols = b.get("volumes") or {}
        cmd, total = media.build_export_cmd(src, segs, out, "mix", vols, encoder, quality, inf, res)
        fb = None
        if encoder != "x264":
            fb, _ = media.build_export_cmd(src, segs, out, "mix", vols, "x264", quality, inf, res)
        jid = uuid.uuid4().hex[:10]
        jobs[jid] = media.ExportJob(cmd, total, out, fb)
        return jsonify(ok=True, job=jid, out=f"Clips/{out.name}")

    @app.post("/api/compile")
    def compile_clips():
        b = request.get_json(force=True) or {}
        files = b.get("files") or []
        if len(files) < 2:
            return jsonify(ok=False, error="Pick at least two videos"), 400
        items = []
        for f in files:
            p = resolve(f)
            inf = info_for(p)
            if not inf.get("duration"):
                return jsonify(ok=False, error=f"Can't read {p.name}"), 400
            items.append((p, inf))
        name = "".join(c for c in (b.get("name") or "").strip() if c not in '<>:"/\\|?*').strip(". ")
        name = name or "Compilation_" + time.strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = out_root() / "Compilations"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.mp4"
        i = 2
        while out.exists():
            out = out_dir / f"{name} ({i}).mp4"
            i += 1
        s = config.load()
        encoder = s["encoder"]
        quality = b.get("quality") or "high"
        transition = b.get("transition") if b.get("transition") in ("cut", "fade", "xfade") else "cut"
        tdur = float(b.get("transition_seconds") or 0.5)
        height = b.get("resolution") or None
        try:
            cmd, total, _ = media.build_compile_cmd(items, out, encoder, quality, transition, tdur, height)
            fb = None
            if encoder != "x264":
                fb, _, _ = media.build_compile_cmd(items, out, "x264", quality, transition, tdur, height)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        jid = uuid.uuid4().hex[:10]
        jobs[jid] = media.ExportJob(cmd, total, out, fb)
        return jsonify(ok=True, job=jid, out=f"Compilations/{out.name}")

    @app.get("/api/export/<jid>")
    def export_status(jid):
        j = jobs.get(jid) or abort(404)
        return jsonify(state=j.state, progress=round(j.progress, 3), error=j.error,
                       out=f"{j.out.parent.name}/{j.out.name}")

    @app.post("/api/export/<jid>/cancel")
    def export_cancel(jid):
        j = jobs.get(jid) or abort(404)
        j.cancel()
        return jsonify(ok=True)

    return app
