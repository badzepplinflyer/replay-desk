"""Audio capture for Replay Desk (Windows).

Game audio = WASAPI loopback of the speakers/headset (pyaudiowpatch).
Mic        = WASAPI input device.

Each source captures continuously into a small buffer. For every FFmpeg run a
PipeFeeder creates a Windows named pipe that FFmpeg reads raw PCM from. The
feeder writes at an exact real-time sample rate: when the source is silent
(loopback delivers no data while nothing plays) it pads with silence, and when
clocks drift it trims the backlog, so audio never drifts out of sync with video.
"""
import ctypes
import logging
import threading
import time

log = logging.getLogger("rd.audio")

try:
    import pyaudiowpatch as pyaudio  # type: ignore
except Exception:  # not on Windows / not installed
    pyaudio = None

JITTER_S = 0.06        # feeder runs this far behind real time to absorb capture jitter
MAX_BACKLOG_S = 0.25   # trim anything beyond this (clock drift)


def available():
    return pyaudio is not None


def list_devices():
    """Return {'outputs': [...], 'inputs': [...]} of WASAPI devices."""
    out = {"outputs": [], "inputs": []}
    if not pyaudio:
        return out
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"]:
                continue
            if d.get("isLoopbackDevice"):
                out["outputs"].append(d["name"].replace(" [Loopback]", ""))
            elif d["maxInputChannels"] > 0:
                out["inputs"].append(d["name"])
    finally:
        p.terminate()
    return out


class AudioSource:
    """Continuously captures one WASAPI device into a byte buffer."""

    def __init__(self, kind, device_name=""):
        self.kind = kind            # "game" or "mic"
        self.device_name = device_name
        self.rate = 48000
        self.channels = 2
        self.bpf = 4                # bytes per frame (s16 * channels)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._pa = None
        self._stream = None
        self.error = None
        self.level = 0.0            # peak 0..1 for the UI meter

    # ---- device selection -------------------------------------------------
    def _pick_device(self, p):
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.kind == "game":
            if self.device_name:
                for lb in p.get_loopback_device_info_generator():
                    if self.device_name in lb["name"]:
                        return lb
            spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if spk.get("isLoopbackDevice"):
                return spk
            for lb in p.get_loopback_device_info_generator():
                if spk["name"] in lb["name"]:
                    return lb
            raise RuntimeError("No loopback device found for " + spk["name"])
        else:
            if self.device_name:
                for i in range(p.get_device_count()):
                    d = p.get_device_info_by_index(i)
                    if (d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0
                            and not d.get("isLoopbackDevice") and self.device_name in d["name"]):
                        return d
            idx = wasapi["defaultInputDevice"]
            if idx < 0:
                raise RuntimeError("No microphone found")
            return p.get_device_info_by_index(idx)

    def start(self):
        if not pyaudio:
            self.error = "pyaudiowpatch not installed"
            return False
        try:
            self._pa = pyaudio.PyAudio()
            dev = self._pick_device(self._pa)
            self.rate = int(dev["defaultSampleRate"])
            self.channels = max(1, min(2, int(dev["maxInputChannels"]))) if self.kind == "mic" \
                else max(1, int(dev["maxInputChannels"]))
            self.bpf = 2 * self.channels
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=int(self.rate * 0.01),
                stream_callback=self._callback,
            )
            self._stream.start_stream()
            log.info("%s audio: %s (%d Hz, %d ch)", self.kind, dev["name"], self.rate, self.channels)
            self.error = None
            return True
        except Exception as e:
            self.error = str(e)
            log.exception("audio start failed (%s)", self.kind)
            self.stop()
            return False

    def _callback(self, in_data, frame_count, time_info, status):
        with self._lock:
            self._buf.extend(in_data)
            cap = int(self.rate * self.bpf * 2)   # never hold more than 2 s
            if len(self._buf) > cap:
                del self._buf[: len(self._buf) - cap]
        # cheap peak meter (sample every 16th value)
        try:
            mv = memoryview(in_data).cast("h")
            peak = max((abs(mv[i]) for i in range(0, len(mv), 16)), default=0)
            self.level = max(peak / 32768.0, self.level * 0.85)
        except Exception:
            pass
        return (None, pyaudio.paContinue)

    def clear(self):
        with self._lock:
            self._buf.clear()

    def take(self, nbytes):
        with self._lock:
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return chunk

    def backlog_bytes(self):
        with self._lock:
            return len(self._buf)

    def trim_to(self, nbytes):
        with self._lock:
            if len(self._buf) > nbytes:
                del self._buf[: len(self._buf) - nbytes]

    def stop(self):
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pa:
                self._pa.terminate()
        except Exception:
            pass
        self._stream = None
        self._pa = None


# ---- Windows named pipes via ctypes (no pywin32 needed) --------------------
if hasattr(ctypes, "windll"):
    k32 = ctypes.windll.kernel32
    k32.CreateNamedPipeW.restype = ctypes.c_void_p
    k32.CreateNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
else:
    k32 = None

PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_PIPE_CONNECTED = 535


class PipeFeeder(threading.Thread):
    """Feeds one AudioSource into one FFmpeg named-pipe input at real-time pace."""

    def __init__(self, source, pipe_name, video_started, get_video_time, offset_s=0.0):
        super().__init__(daemon=True)
        self.source = source
        self.path = "\\\\.\\pipe\\" + pipe_name
        self.video_started = video_started      # threading.Event set when FFmpeg reports frame > 0
        self.get_video_time = get_video_time    # callable -> seconds of video encoded so far
        self.offset_s = offset_s
        self._stop = threading.Event()
        self.handle = None
        self.connected = threading.Event()
        self.error = None

    def create(self):
        self.handle = k32.CreateNamedPipeW(self.path, PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT,
                                           1, 1 << 16, 0, 0, None)
        if self.handle in (None, INVALID_HANDLE):
            raise OSError("CreateNamedPipe failed for " + self.path)

    def _write(self, data):
        written = ctypes.c_uint32(0)
        ok = k32.WriteFile(self.handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            raise BrokenPipeError
        return written.value

    def stop(self):
        self._stop.set()
        if not self.connected.is_set():
            # FFmpeg never opened the pipe: connect to it ourselves so run() can exit
            try:
                with open(self.path, "rb"):
                    pass
            except OSError:
                pass

    def run(self):
        try:
            ok = k32.ConnectNamedPipe(self.handle, None)
            if not ok and ctypes.GetLastError() != ERROR_PIPE_CONNECTED:
                raise OSError("ConnectNamedPipe failed")
            self.connected.set()
            src = self.source
            rate, bpf = src.rate, src.bpf
            start = time.perf_counter()
            written = 0          # frames written into the pipe
            hold_until = 0.0     # used to re-align once video starts
            aligned = False
            while not self._stop.is_set():
                now = time.perf_counter()
                if not aligned and self.video_started.is_set():
                    # Line audio up with video: audio written so far vs video encoded so far.
                    a_t = written / rate
                    v_t = self.get_video_time() + self.offset_s
                    diff = a_t - v_t
                    src.clear()
                    if diff > 0:            # audio ahead: pause writing for `diff` seconds
                        hold_until = now + diff
                        start += diff
                    elif diff < 0:          # audio behind: insert silence
                        pad = int(-diff * rate)
                        self._write(b"\x00" * (pad * bpf))
                        written += pad
                        start -= pad / rate
                    aligned = True
                    continue
                if now < hold_until:
                    src.clear()             # discard audio captured before the video catches up
                    time.sleep(0.005)
                    continue
                target = int((now - start - JITTER_S) * rate)
                need = target - written
                if need <= 0:
                    time.sleep(0.005)
                    continue
                if not aligned:
                    data = b"\x00" * (need * bpf)       # silence until video is running
                    src.clear()
                else:
                    data = src.take(need * bpf)
                    if len(data) < need * bpf:           # source starved (silence) -> pad
                        data += b"\x00" * (need * bpf - len(data))
                    src.trim_to(int(MAX_BACKLOG_S * rate) * bpf)
                self._write(data)
                written += need
        except BrokenPipeError:
            pass
        except Exception as e:
            self.error = str(e)
            log.exception("pipe feeder error")
        finally:
            try:
                if self.handle not in (None, INVALID_HANDLE):
                    k32.DisconnectNamedPipe(self.handle)
                    k32.CloseHandle(self.handle)
            except Exception:
                pass
