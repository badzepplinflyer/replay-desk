"""Capture engine: one always-running FFmpeg writes 2-second segments.

* Replay buffer  = keep the newest N seconds of segments, delete older ones.
* Save replay    = stitch the last N seconds of segments into an MP4 (no re-encode).
* Manual record  = remember the segment where recording started, stitch everything
                   from there to the stop point. Segments in a recording are kept.

Video: FFmpeg ddagrab (Windows Desktop Duplication) -> NVENC on the GPU.
Audio: track 1 = game+mic mix, track 2 = game only, track 3 = mic only.
"""
import csv
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, media
from .audio import AudioSource, PipeFeeder, available as audio_available
from .hotkeys import game_label

log = logging.getLogger("rd.recorder")

SEG_SECONDS = 2


class Segment:
    __slots__ = ("path", "start", "end", "run")

    def __init__(self, path, start, end, run):
        self.path, self.start, self.end, self.run = path, start, end, run


class Recorder:
    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda kind, msg, **kw: None)
        self.settings = config.load()
        self.proc = None
        self.run_id = 0
        self.segments = []              # completed segments, oldest first
        self.lock = threading.RLock()
        self.running = False            # buffer armed
        self.want_running = False
        self.recording_from = None      # index into self.segments history (absolute counter)
        self.recording_started_at = None
        self.seg_counter = 0            # absolute count of completed segments ever
        self.fps_live = 0.0
        self.frames = 0
        self.drop = 0
        self.last_error = None
        self.video_started = threading.Event()
        self.sources = {}
        self.feeders = []
        self._saving = threading.Lock()
        self._stderr_tail = []
        self._supervisor = None

    # ------------------------------------------------------------------ state
    def status(self):
        with self.lock:
            buffered = sum(s.end - s.start for s in self.segments[-self._buffer_count():])
            rec_secs = None
            if self.recording_started_at:
                rec_secs = time.time() - self.recording_started_at
            return {
                "buffer_running": self.running,
                "buffer_seconds": round(buffered, 1),
                "buffer_target": self.settings["buffer_seconds"],
                "recording": self.recording_started_at is not None,
                "recording_seconds": round(rec_secs, 1) if rec_secs else 0,
                "fps": round(self.fps_live, 1),
                "dropped": self.drop,
                "error": self.last_error,
                "ffmpeg": config.FFMPEG,
                "audio": {k: {"ok": s.error is None, "error": s.error, "level": round(s.level, 3),
                              "rate": s.rate, "channels": s.channels}
                          for k, s in self.sources.items()},
                "audio_lib": audio_available(),
            }

    def _buffer_count(self):
        return max(1, int(self.settings["buffer_seconds"] / SEG_SECONDS + 0.999))

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self.want_running:
            return
        self.settings = config.load()
        config.ensure_dirs(self.settings)
        if not config.FFMPEG:
            self.last_error = "FFmpeg not found - reinstall Replay Desk"
            self.on_event("error", self.last_error)
            return
        self.want_running = True
        self._start_audio()
        self._supervisor = threading.Thread(target=self._supervise, daemon=True)
        self._supervisor.start()

    def stop(self):
        self.want_running = False
        if self.recording_started_at:
            self.stop_recording()
        self._kill_ffmpeg()
        for s in self.sources.values():
            s.stop()
        self.sources = {}
        self.running = False

    def restart(self):
        """Apply new settings (keeps buffered segments only within the same run)."""
        was = self.want_running
        self.stop()
        if was:
            time.sleep(0.5)
            self.start()

    def _start_audio(self):
        self.sources = {}
        s = self.settings
        if not audio_available():
            return
        if s["game_audio"]:
            src = AudioSource("game", s["game_device"])
            src.start()
            self.sources["game"] = src
        if s["mic_enabled"]:
            src = AudioSource("mic", s["mic_device"])
            src.start()
            self.sources["mic"] = src

    def _kill_ffmpeg(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.wait(4)
            except Exception:
                p.kill()
        for f in self.feeders:
            f.stop()
        self.feeders = []
        self.proc = None

    # ------------------------------------------------------------ supervisor
    def _supervise(self):
        failures = 0
        while self.want_running:
            started = time.time()
            try:
                self._run_ffmpeg_once()
            except Exception as e:
                log.exception("ffmpeg run failed")
                self.last_error = str(e)
            self.running = False
            if not self.want_running:
                break
            # ddagrab exits when a game changes resolution / display mode: just restart.
            failures = failures + 1 if time.time() - started < 10 else 0
            if failures >= 5:
                self.last_error = "Capture keeps failing:\n" + "\n".join(self._stderr_tail[-6:])
                self.on_event("error", "Capture stopped - see Recorder tab for details")
                self.want_running = False
                break
            time.sleep(min(5, 0.5 * (failures + 1)))

    def _active_sources(self):
        return {k: s for k, s in self.sources.items() if s.error is None}

    def _build_cmd(self, run_dir, pipes):
        s = self.settings
        fps = int(s["fps"])
        cq = config.QUALITY_CQ.get(s["quality"], 21)
        cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-stats_period", "0.1"]
        dda = f"ddagrab=output_idx={int(s['monitor'])}:framerate={fps}:draw_mouse=0"
        if s["encoder"] == "x264":
            dda += ",hwdownload,format=bgra,format=yuv420p"
        cmd += ["-f", "lavfi", "-i", dda]
        n_in = 1
        audio_idx = {}
        for kind, (src, pipe) in pipes.items():
            cmd += ["-f", "s16le", "-ar", str(src.rate), "-ac", str(src.channels),
                    "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "4096",
                    "-i", pipe]
            audio_idx[kind] = n_in
            n_in += 1

        maps = ["-map", "0:v"]
        fc = []
        norm = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if "game" in audio_idx and "mic" in audio_idx:
            fc.append(f"[{audio_idx['game']}:a]{norm},asplit=2[g1][g2]")
            fc.append(f"[{audio_idx['mic']}:a]{norm},volume={float(s['mic_volume']):.2f},asplit=2[m1][m2]")
            fc.append("[g1][m1]amix=inputs=2:normalize=0:duration=longest[mix]")
            maps += ["-map", "[mix]", "-map", "[g2]", "-map", "[m2]"]
            titles = ["Game + Mic", "Game", "Mic"]
        elif audio_idx:
            kind = next(iter(audio_idx))
            fc.append(f"[{audio_idx[kind]}:a]{norm}[a0]")
            maps += ["-map", "[a0]"]
            titles = ["Game" if kind == "game" else "Mic"]
        else:
            titles = []
        if fc:
            cmd += ["-filter_complex", ";".join(fc)]
        cmd += maps

        if s["encoder"] == "x264":
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cq + 1)]
        elif s["encoder"] == "amf":
            cmd += ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                    "-qp_i", str(cq), "-qp_p", str(cq + 2)]
        else:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-profile:v", "high",
                    "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-maxrate", "100M", "-bufsize", "200M",
                    "-bf", "0"]
        cmd += ["-g", str(fps * SEG_SECONDS), "-keyint_min", str(fps * SEG_SECONDS),
                "-force_key_frames", f"expr:gte(t,n_forced*{SEG_SECONDS})"]
        if titles:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
            for i, t in enumerate(titles):
                cmd += [f"-metadata:s:a:{i}", f"title={t}"]
        cmd += ["-max_muxing_queue_size", "8192",
                "-f", "segment", "-segment_time", str(SEG_SECONDS), "-segment_format", "mpegts",
                "-segment_list", str(run_dir / "list.csv"), "-segment_list_type", "csv",
                str(run_dir / "seg_%06d.ts")]
        return cmd

    def _run_ffmpeg_once(self):
        self.run_id += 1
        run = self.run_id
        run_dir = config.BUFFER_DIR / f"run_{os.getpid()}_{run:04d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.video_started.clear()
        self.frames = 0

        pipes, feeders = {}, []
        offset = float(self.settings["audio_offset_ms"]) / 1000.0
        for kind, src in self._active_sources().items():
            name = f"replaydesk_{kind}_{os.getpid()}_{run}"
            f = PipeFeeder(src, name, self.video_started, self._video_time, offset)
            f.create()
            pipes[kind] = (src, f.path)
            feeders.append(f)

        cmd = self._build_cmd(run_dir, pipes)
        log.info("ffmpeg: %s", subprocess.list2cmdline(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=config.CREATE_NO_WINDOW)
        for f in feeders:
            f.start()
        self.feeders = feeders
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._watch_segments, args=(self.proc, run_dir, run), daemon=True).start()

        self._read_progress(self.proc)       # blocks until ffmpeg exits
        self.proc.wait()
        for f in feeders:
            f.stop()
        code = self.proc.returncode
        if code not in (0, None) and self.want_running:
            log.warning("ffmpeg exited %s: %s", code, " | ".join(self._stderr_tail[-4:]))

    def _video_time(self):
        return self.frames / max(1, int(self.settings["fps"]))

    def _read_progress(self, proc):
        fps_window = []
        for raw in proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("frame="):
                try:
                    self.frames = int(line[6:])
                except ValueError:
                    continue
                if self.frames > 0 and not self.video_started.is_set():
                    self.video_started.set()
                    self.running = True
                    self.last_error = None
                    self.on_event("buffer", "Replay buffer running")
                now = time.time()
                fps_window.append((now, self.frames))
                fps_window = [x for x in fps_window if now - x[0] < 2]
                if len(fps_window) > 1:
                    dt = fps_window[-1][0] - fps_window[0][0]
                    if dt > 0:
                        self.fps_live = (fps_window[-1][1] - fps_window[0][1]) / dt
            elif line.startswith("drop_frames="):
                try:
                    self.drop = int(line[12:])
                except ValueError:
                    pass

    def _read_stderr(self, proc):
        for raw in proc.stderr:
            line = raw.decode("utf-8", "ignore").rstrip()
            if line:
                self._stderr_tail = (self._stderr_tail + [line])[-30:]
                log.warning("ffmpeg: %s", line)

    def _watch_segments(self, proc, run_dir, run):
        """Tail ffmpeg's segment list; prune segments outside buffer/recording."""
        list_file = run_dir / "list.csv"
        seen = 0
        while True:
            alive = proc.poll() is None
            if list_file.exists():
                try:
                    with open(list_file, newline="") as fh:
                        rows = list(csv.reader(fh))
                except OSError:
                    rows = []
                for row in rows[seen:]:
                    if len(row) < 3:
                        continue
                    seg = Segment(run_dir / row[0], float(row[1]), float(row[2]), run)
                    with self.lock:
                        self.segments.append(seg)
                        self.seg_counter += 1
                    seen += 1
                self._prune()
            if not alive:
                break
            time.sleep(0.25)

    def _prune(self):
        with self.lock:
            keep_n = self._buffer_count() + 2
            protect_from = None
            if self.recording_from is not None:
                # absolute counter -> index into list
                protect_from = self.recording_from - (self.seg_counter - len(self.segments))
            drop = []
            while len(self.segments) > keep_n and (protect_from is None or protect_from > 0):
                drop.append(self.segments.pop(0))
                if protect_from is not None:
                    protect_from -= 1
            for seg in drop:
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError:
                    pass

    # --------------------------------------------------------------- actions
    def _wait_next_segment(self, timeout=SEG_SECONDS * 2 + 1):
        """Wait for the segment currently being written to close, so the clip
        includes the moment the hotkey was pressed."""
        with self.lock:
            target = self.seg_counter + 1
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.seg_counter >= target:
                    return True
            time.sleep(0.1)
        return False

    def save_replay(self):
        if not self.running:
            self.on_event("error", "Replay buffer isn't running")
            return None
        if not self._saving.acquire(blocking=False):
            return None
        try:
            label = game_label() if self.settings["name_by_game"] else None
            self._wait_next_segment()
            with self.lock:
                segs = list(self.segments[-self._buffer_count():])
            if not segs:
                self.on_event("error", "Nothing buffered yet")
                return None
            out = self._output_path("Replays", "Replay", label)
            media.concat_segments([s.path for s in segs], out)
            media.make_thumbnail(out)
            self.on_event("saved", f"Replay saved: {out.name}", path=str(out), sound="replay")
            return out
        except Exception as e:
            log.exception("save replay failed")
            self.on_event("error", f"Saving replay failed: {e}")
            return None
        finally:
            self._saving.release()

    def toggle_recording(self):
        if self.recording_started_at:
            return self.stop_recording()
        return self.start_recording()

    def start_recording(self):
        if not self.running:
            self.on_event("error", "Capture isn't running")
            return None
        with self.lock:
            # the segment being written right now is the first one of the recording
            self.recording_from = self.seg_counter
            self.recording_started_at = time.time()
            self._rec_label = game_label() if self.settings["name_by_game"] else None
        self.on_event("recording", "Recording started", sound="rec_start")
        return True

    def stop_recording(self):
        if not self.recording_started_at:
            return None
        self._wait_next_segment()
        with self.lock:
            idx = self.recording_from - (self.seg_counter - len(self.segments))
            segs = list(self.segments[max(0, idx):])
            self.recording_from = None
            self.recording_started_at = None
        self.on_event("recording_stopped", "Recording stopped - saving...", sound="rec_stop")
        try:
            out = self._output_path("Recordings", "Recording", getattr(self, "_rec_label", None))
            media.concat_segments([s.path for s in segs], out)
            media.make_thumbnail(out)
            self.on_event("saved", f"Recording saved: {out.name}", path=str(out))
            self._prune()
            return out
        except Exception as e:
            log.exception("save recording failed")
            self.on_event("error", f"Saving recording failed: {e}")
            return None

    def _output_path(self, folder, kind, label):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{label}_{stamp}" if label else f"{kind}_{stamp}"
        out_dir = Path(self.settings["output_dir"]) / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{name}.mp4"


def clean_buffer_dir():
    """Remove leftovers from previous sessions."""
    import shutil
    if config.BUFFER_DIR.exists():
        for d in config.BUFFER_DIR.iterdir():
            shutil.rmtree(d, ignore_errors=True)
