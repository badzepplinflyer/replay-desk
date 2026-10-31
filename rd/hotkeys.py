"""Global hotkeys via Win32 RegisterHotKey (works while a game has focus)."""
import ctypes
import logging
import threading

log = logging.getLogger("rd.hotkeys")

MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8000 + 1

VK = {f"f{i}": 0x6F + i for i in range(1, 25)}
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({"home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "pageup": 0x21,
           "pagedown": 0x22, "pause": 0x13, "scrolllock": 0x91, "printscreen": 0x2C,
           "space": 0x20, "tab": 0x09})
for i in range(10):
    VK[f"num{i}"] = 0x60 + i


def parse(combo):
    """'Ctrl+Alt+F10' -> (mods, vk). Raises ValueError."""
    mods, vk = 0, None
    for part in combo.replace(" ", "").split("+"):
        p = part.lower()
        if p in MOD:
            mods |= MOD[p]
        elif p in VK:
            vk = VK[p]
        else:
            raise ValueError(f"Unknown key '{part}'")
    if vk is None:
        raise ValueError("Hotkey needs a non-modifier key")
    return mods, vk


class HotkeyManager(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.bindings = {}      # id -> (combo, callback)
        self.status = {}        # combo -> "ok" | error text
        self._pending = None
        self._thread_id = None
        self._ready = threading.Event()
        self.enabled = hasattr(ctypes, "windll")

    def set_bindings(self, mapping):
        """mapping: {combo: callback}. Safe to call from any thread."""
        self._pending = dict(mapping)
        if not self.enabled:
            self.status = {c: "hotkeys only work on Windows" for c in mapping}
            return
        self._ready.wait(3)
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP_RELOAD, 0, 0)

    def _apply(self):
        user32 = ctypes.windll.user32
        for hid in list(self.bindings):
            user32.UnregisterHotKey(None, hid)
        self.bindings.clear()
        self.status = {}
        for i, (combo, cb) in enumerate((self._pending or {}).items(), start=1):
            try:
                mods, vk = parse(combo)
                if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    self.bindings[i] = (combo, cb)
                    self.status[combo] = "ok"
                else:
                    self.status[combo] = "in use by another app (e.g. NVIDIA overlay) - pick another"
            except ValueError as e:
                self.status[combo] = str(e)
        log.info("hotkeys: %s", self.status)

    def run(self):
        if not self.enabled:
            return
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # force creation of the thread message queue before signalling ready
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()
        if self._pending:
            self._apply()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                b = self.bindings.get(msg.wParam)
                if b:
                    threading.Thread(target=self._safe, args=(b[1],), daemon=True).start()
            elif msg.message == WM_APP_RELOAD:
                self._apply()

    @staticmethod
    def _safe(cb):
        try:
            cb()
        except Exception:
            log.exception("hotkey callback failed")


def foreground_app_name():
    """Name of the exe that has focus (used to name clips after the game)."""
    if not hasattr(ctypes, "windll"):
        return None
    try:
        from ctypes import wintypes
        user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        if not ok:
            return None
        exe = buf.value.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
        return exe
    except Exception:
        return None


FRIENDLY = {
    "iracingsim64dx11": "iRacing",
    "flightsimulator2024": "MSFS2024",
    "flightsimulator": "MSFS",
    "explorer": None, "pythonw": None, "python": None, "msedge": None, "chrome": None,
}


def game_label():
    exe = foreground_app_name()
    if not exe:
        return None
    key = exe.lower()
    if key in FRIENDLY:
        return FRIENDLY[key]
    clean = "".join(ch for ch in exe if ch.isalnum() or ch in "-_")
    return clean[:32] or None
