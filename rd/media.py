"""FFmpeg helpers: stitching segments, probing, thumbnails, filmstrips, clip export."""
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from . import config

log = logging.getLogger("rd.media")


def _run(cmd, timeout=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=config.CREATE_NO_WINDOW, timeout=timeout)
    if r.returncode != 0:
        lines = r.stderr.decode("utf-8", "ignore").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "ffmpeg failed")
    return r


def concat_segments(paths, out):
    """Join .ts segments into an MP4 without re-encoding."""
    out = Path(out)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        for p in paths:
            fh.write("file '" + str(p).replace("\\", "/").replace("'", "'\\''") + "'\n")
        lst = fh.name
    tmp = out.with_suffix(".part.mp4")
    meta = []
    try:   # MPEG-TS drops track titles; put them back for players that show them
        n_audio = len(probe(paths[0])["audio_tracks"])
        titles = ["Game + Mic", "Game", "Mic"] if n_audio == 3 else []
        for i, t in enumerate(titles):
            meta += [f"-metadata:s:a:{i}", f"title={t}"]
    except Exception:
        pass
    try:
        _run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", lst,
              "-map", "0", "-c", "copy", *meta, "-movflags", "+faststart", str(tmp)])
        os.replace(tmp, out)
    finally:
        try:
            os.unlink(lst)
        except OSError:
            pass
        if tmp.exists():
            tmp.unlink()
    return out


def probe(path):
    r = _run([config.FFPROBE, "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", str(path)])
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    audio = [s for s in d["streams"] if s["codec_type"] == "audio"]
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        n, dd = fr.split("/")
        fps = float(n) / float(dd) if float(dd) else 0
    except Exception:
        fps = 0
    return {
        "duration": float(d["format"].get("duration", 0) or 0),
        "size": int(d["format"].get("size", 0) or 0),
        "width": v.get("width"), "height": v.get("height"), "fps": round(fps, 2),
        "audio_tracks": [{"index": i, "title": (a.get("tags") or {}).get("title") or f"Track {i + 1}",
                          "channels": a.get("channels")} for i, a in enumerate(audio)],
    }


def _cache_key(path, extra=""):
    st = Path(path).stat()
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}|{extra}".encode()).hexdigest()[:16]


def thumb_path(video):
    return Path(video).with_suffix(".jpg").parent / ".thumbs" / (Path(video).stem + ".jpg")


def make_thumbnail(video, at=None):
    video = Path(video)
    out = thumb_path(video)
    out.parent.mkdir(exist_ok=True)
    if os.name == "nt":
        try:  # hide the .thumbs folder in Explorer
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(out.parent), 0x02)
        except Exception:
            pass
    try:
        dur = probe(video)["duration"]
    except Exception:
        dur = 0
    t = at if at is not None else max(0, dur * 0.5)
    try:
        _run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
              "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)], timeout=60)
    except Exception:
        log.warning("thumbnail failed for %s", video)
    return out


_strip_lock = threading.Lock()


def filmstrip(video, count=40, height=72):
    """One sprite image of `count` evenly spaced frames (keyframes only = fast)."""
    key = _cache_key(video, f"{count}x{height}")
    out = config.CACHE_DIR / f"strip_{key}.jpg"
    if out.exists():
        return out
    with _strip_lock:
        if out.exists():
            return out
        dur = probe(video)["duration"] or 1
        rate = (count + 2) / dur      # slightly over so every tile gets filled
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
              "-skip_frame", "nokey", "-i", str(video),
              "-vf", f"fps={rate:.6f},scale=-2:{height},tile={count}x1",
              "-frames:v", "1", "-q:v", "5", str(out)], timeout=300)
    return out


def encoder_args(encoder, quality="high"):
    cq = {"high": 18, "medium": 22, "low": 26}.get(quality, 20)
    if encoder == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-profile:v", "high",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if encoder == "amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(cq), "-qp_p", str(cq + 2), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(cq), "-pix_fmt", "yuv420p"]


def build_export_cmd(src, segments, out, audio_mode, volumes, encoder, quality, info,
                     resolution=None):
    """segments: [(start, end), ...] in play order.
    Each segment is opened as its own input with a fast, frame-accurate seek (-ss before -i),
    so cutting from the end of an hour-long recording doesn't decode the whole file.
    volumes: {track_index: gain} -> tracks are mixed into one stereo track.
    """
    tracks = info["audio_tracks"]
    has_split = len(tracks) >= 3        # Mix / Game / Mic layout from the recorder
    vol = lambda i: float(volumes.get(str(i), volumes.get(i, 1.0)))
    if not tracks:
        use_audio = []
    elif has_split:
        use_audio = [i for i in (1, 2) if vol(i) > 0]
    else:
        use_audio = [0] if vol(0) > 0 else []

    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1"]
    fc, pairs = [], []
    for n, (a, b) in enumerate(segments):
        if encoder != "x264":
            cmd += ["-hwaccel", "auto"]
        cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
        vf = f"[{n}:v]setpts=PTS-STARTPTS"
        if resolution:
            vf += f",scale=-2:{int(resolution)}:flags=lanczos"
        vf += ",format=yuv420p"
        fc.append(vf + f"[v{n}]")
        pair = f"[v{n}]"
        if use_audio:
            parts = []
            for ti in use_audio:
                lbl = f"a{n}_{ti}"
                fc.append(f"[{n}:a:{ti}]asetpts=PTS-STARTPTS,volume={vol(ti):.2f}[{lbl}]")
                parts.append(f"[{lbl}]")
            if len(parts) > 1:
                fc.append("".join(parts) + f"amix=inputs={len(parts)}:normalize=0:duration=first[a{n}]")
            else:
                fc.append(parts[0] + f"anull[a{n}]")
            pair += f"[a{n}]"
        pairs.append(pair)

    k = len(segments)
    if use_audio:
        fc.append("".join(pairs) + f"concat=n={k}:v=1:a=1[vout][aout]")
    else:
        fc.append("".join(pairs) + f"concat=n={k}:v=1:a=0[vout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]"]
    if use_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    cmd += encoder_args(encoder, quality)
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd, sum(b - a for a, b in segments)


def build_compile_cmd(items, out, encoder, quality, transition="cut", tdur=0.5, height=None, fps=None):
    """Join whole videos into one.
    items: [(path, info), ...] in order (info from probe()).
    Every clip is scaled/padded to one size and frame rate, audio is normalised to
    48 kHz stereo (track 1 = the mixed track), and clips without audio get silence.
    transition: 'cut' | 'fade' (dip to black) | 'xfade' (crossfade).
    """
    if not items:
        raise ValueError("No clips")
    # output size: chosen height at the widest aspect used, else the biggest clip
    if height:
        H = int(height)
        W = int(round(H * 16 / 9 / 2) * 2)
    else:
        big = max(items, key=lambda it: (it[1].get("width") or 0) * (it[1].get("height") or 0))[1]
        W, H = int(big.get("width") or 1920), int(big.get("height") or 1080)
        W, H = W - W % 2, H - H % 2
    F = float(fps or min(120, max(it[1].get("fps") or 60 for it in items)))
    durs = [max(0.1, float(it[1].get("duration") or 0)) for it in items]
    if transition != "cut":
        tdur = max(0.1, min(float(tdur), min(durs) / 2 - 0.05))

    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1"]
    fc = []
    for n, (path, info) in enumerate(items):
        if encoder != "x264":
            cmd += ["-hwaccel", "auto"]
        cmd += ["-i", str(path)]
        D = durs[n]
        v = (f"[{n}:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={F:g},format=yuv420p,"
             f"trim=duration={D:.3f},setpts=PTS-STARTPTS")
        if info.get("audio_tracks"):
            a = (f"[{n}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"asetpts=PTS-STARTPTS,apad,atrim=duration={D:.3f}")
        else:
            a = f"anullsrc=r=48000:cl=stereo,atrim=duration={D:.3f}"
        if transition == "fade":
            v += f",fade=t=in:st=0:d={tdur:.3f},fade=t=out:st={D - tdur:.3f}:d={tdur:.3f}"
            a += f",afade=t=in:st=0:d={tdur:.3f},afade=t=out:st={D - tdur:.3f}:d={tdur:.3f}"
        fc.append(v + f"[v{n}]")
        fc.append(a + f"[a{n}]")

    k = len(items)
    if transition == "xfade" and k > 1:
        vprev, aprev, length = "[v0]", "[a0]", durs[0]
        for n in range(1, k):
            off = length - tdur
            vo, ao = (f"[vx{n}]", f"[ax{n}]") if n < k - 1 else ("[vout]", "[aout]")
            fc.append(f"{vprev}[v{n}]xfade=transition=fade:duration={tdur:.3f}:offset={off:.3f}{vo}")
            fc.append(f"{aprev}[a{n}]acrossfade=d={tdur:.3f}{ao}")
            vprev, aprev = vo, ao
            length = length + durs[n] - tdur
        total = length
    else:
        fc.append("".join(f"[v{n}][a{n}]" for n in range(k)) + f"concat=n={k}:v=1:a=1[vout][aout]")
        total = sum(durs)

    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k"]
    cmd += encoder_args(encoder, quality)
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd, total, (W, H, F)


class ExportJob:
    def __init__(self, cmd, total, out, fallback_cmd=None):
        self.cmd, self.total, self.out = cmd, total, Path(out)
        self.fallback_cmd = fallback_cmd
        self.progress = 0.0
        self.state = "running"
        self.error = None
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _attempt(self, cmd):
        log.info("export: %s", subprocess.list2cmdline(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=config.CREATE_NO_WINDOW)
        err = []
        t = threading.Thread(target=lambda: err.extend(self.proc.stderr.read().decode("utf-8", "ignore")
                                                       .splitlines()), daemon=True)
        t.start()
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    self.progress = min(0.99, us / 1e6 / max(0.01, self.total))
                except ValueError:
                    pass
        self.proc.wait()
        t.join(2)
        return self.proc.returncode, err

    def _run(self):
        try:
            code, err = self._attempt(self.cmd)
            if code != 0 and self.state != "cancelled" and self.fallback_cmd:
                log.warning("export failed with GPU encoder, retrying on CPU: %s", err[-3:])
                code, err = self._attempt(self.fallback_cmd)
            if self.state == "cancelled":
                self.out.unlink(missing_ok=True)
                return
            if code != 0:
                self.state, self.error = "error", (err[-1] if err else "ffmpeg failed")
                self.out.unlink(missing_ok=True)
                return
            make_thumbnail(self.out)
            self.progress, self.state = 1.0, "done"
        except Exception as e:
            self.state, self.error = "error", str(e)

    def cancel(self):
        self.state = "cancelled"
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
