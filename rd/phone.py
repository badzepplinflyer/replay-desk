"""Send to phone: share a video over the home Wi-Fi with a QR code.

A second, tiny web server listens on the local network (port 47821) and serves
ONLY the videos you chose to share, each behind a random link that expires.
The phone scans the QR code, opens a simple page and downloads/saves the video.
Optionally a phone-friendly copy (max 1080p / 60 fps, one audio track) is made first.
"""
import hashlib
import io
import logging
import secrets
import socket
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, send_file

from . import config, media

log = logging.getLogger("rd.phone")

PHONE_PORT = 47821
SHARE_MINUTES = 60


def lan_ip():
    """IP address of this PC on the home network (no traffic is actually sent)."""
    for target in ("192.168.0.1", "10.255.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    return ip
        except OSError:
            continue
    return None


def needs_phone_copy(info):
    return (info.get("height") or 0) > 1080 or (info.get("fps") or 0) > 61 \
        or len(info.get("audio_tracks") or []) > 1


def phone_copy_cmd(src, out, info, encoder):
    h = min(1080, int(info.get("height") or 1080))
    fps = min(60, info.get("fps") or 60)
    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1"]
    if encoder != "x264":
        cmd += ["-hwaccel", "auto"]
    cmd += ["-i", str(src), "-map", "0:v:0"]
    if info.get("audio_tracks"):
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    cmd += ["-vf", f"scale=-2:{h}:flags=lanczos,fps={fps:g},format=yuv420p"]
    cmd += media.encoder_args(encoder, "medium")
    if encoder == "nvenc":
        cmd += ["-maxrate", "16M", "-bufsize", "32M"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


class Share:
    def __init__(self, src, name):
        self.id = secrets.token_urlsafe(9)
        self.token = secrets.token_urlsafe(16)
        self.src = Path(src)
        self.name = name
        self.file = None              # what the phone downloads
        self.state = "preparing"      # preparing | ready | error | stopped
        self.progress = 0.0
        self.error = None
        self.job = None
        self.created = time.time()
        self.expires = self.created + SHARE_MINUTES * 60
        self.downloads = 0

    def expired(self):
        return time.time() > self.expires or self.state == "stopped"


class PhoneShare:
    def __init__(self):
        self.shares = {}              # id -> Share
        self._server = None
        self._lock = threading.Lock()
        self.server_error = None

    # ------------------------------------------------------------ LAN server
    def _ensure_server(self):
        with self._lock:
            if self._server:
                return True
            app = self._make_app()
            try:
                from werkzeug.serving import make_server
                self._server = make_server("0.0.0.0", PHONE_PORT, app, threaded=True)
            except OSError as e:
                self.server_error = f"Couldn't open the phone link port {PHONE_PORT}: {e}"
                log.error(self.server_error)
                return False
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            log.info("phone share server on port %d", PHONE_PORT)
            return True

    def _by_token(self, token):
        for s in self.shares.values():
            if s.token == token and not s.expired():
                return s
        return None

    def _make_app(self):
        app = Flask("replaydesk-phone")
        page_tpl = (config.APP_DIR / "web" / "phone.html").read_text("utf-8")

        @app.get("/v/<token>")
        def page(token):
            s = self._by_token(token)
            if not s:
                return ("<meta name=viewport content='width=device-width'><body style='font-family:sans-serif;"
                        "background:#0d0f13;color:#ccc;padding:40px;text-align:center'>"
                        "<h2>This link has expired</h2><p>Click 📱 again in Replay Desk for a new one.</p>"), 404
            if s.state != "ready":
                return ("<meta http-equiv=refresh content=2><meta name=viewport content='width=device-width'>"
                        "<body style='font-family:sans-serif;background:#0d0f13;color:#ccc;padding:40px;text-align:center'>"
                        f"<h2>Getting your video ready… {int(s.progress * 100)}%</h2></body>")
            size = s.file.stat().st_size
            html = (page_tpl.replace("{{NAME}}", _esc(s.name))
                    .replace("{{SIZE}}", f"{size / 1e6:.0f} MB")
                    .replace("{{TOKEN}}", token))
            return html

        @app.get("/v/<token>/video.mp4")
        def stream(token):
            s = self._by_token(token) or abort(404)
            if s.state != "ready":
                abort(404)
            return send_file(s.file, mimetype="video/mp4", conditional=True)

        @app.get("/v/<token>/download")
        def download(token):
            s = self._by_token(token) or abort(404)
            if s.state != "ready":
                abort(404)
            s.downloads += 1
            return send_file(s.file, mimetype="video/mp4", as_attachment=True,
                             download_name=_safe_name(s.name) + ".mp4", conditional=True)

        @app.get("/<path:_>")
        @app.get("/")
        def nothing(_=None):
            abort(404)        # nothing else on this PC is reachable from the network

        return app

    # ------------------------------------------------------------- sharing
    def create(self, src, optimize, encoder):
        if not self._ensure_server():
            raise RuntimeError(self.server_error)
        self._cleanup()
        info = media.probe(src)
        share = Share(src, Path(src).stem)
        self.shares[share.id] = share
        if optimize and needs_phone_copy(info):
            st = Path(src).stat()
            key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
            out = config.CACHE_DIR / f"phone_{key}.mp4"
            if out.exists():
                share.file, share.state, share.progress = out, "ready", 1.0
            else:
                config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(out.stem + ".part.mp4")
                cmd = phone_copy_cmd(src, tmp, info, encoder)
                fb = phone_copy_cmd(src, tmp, info, "x264") if encoder != "x264" else None
                share.job = media.ExportJob(cmd, info.get("duration") or 1, tmp, fb, thumbnail=False)
                threading.Thread(target=self._watch_job, args=(share, tmp, out), daemon=True).start()
        else:
            share.file, share.state, share.progress = Path(src), "ready", 1.0
        return share

    def _watch_job(self, share, tmp, out):
        while share.job.state == "running":
            share.progress = share.job.progress
            time.sleep(0.3)
        if share.job.state == "done" and tmp.exists():
            tmp.replace(out)
            share.file, share.state, share.progress = out, "ready", 1.0
        elif share.state != "stopped":
            share.state, share.error = "error", share.job.error or "Couldn't make the phone copy"

    def status(self, sid):
        s = self.shares.get(sid)
        if not s:
            return None
        ip = lan_ip()
        url = f"http://{ip}:{PHONE_PORT}/v/{s.token}" if ip else None
        return {"id": s.id, "state": "expired" if s.expired() and s.state != "stopped" else s.state,
                "progress": round(s.progress, 3), "error": s.error, "url": url,
                "expires_in": max(0, int(s.expires - time.time())), "downloads": s.downloads,
                "name": s.name, "size": s.file.stat().st_size if s.file and s.file.exists() else None,
                "no_network": ip is None}

    def qr_png(self, sid):
        st = self.status(sid)
        if not st or not st["url"]:
            return None
        import qrcode
        qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(st["url"])
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    def stop(self, sid):
        s = self.shares.get(sid)
        if s:
            s.state = "stopped"
            if s.job:
                s.job.cancel()

    def _cleanup(self):
        for sid, s in list(self.shares.items()):
            if s.expired() and time.time() - s.expires > 600:
                self.shares.pop(sid, None)
        # drop phone copies older than a day
        for p in config.CACHE_DIR.glob("phone_*.mp4"):
            try:
                if time.time() - p.stat().st_mtime > 86400:
                    p.unlink()
            except OSError:
                pass


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _safe_name(s):
    return "".join(c for c in s if c.isalnum() or c in " -_()").strip() or "ReplayDesk-video"
