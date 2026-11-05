"""Floating always-on-top control panel ("overlay").

Toggle with the overlay hotkey (default Ctrl+Alt+O) or from the tray menu.
It stays above borderless/windowed games and clicking its buttons does not take
focus away from the game (WS_EX_NOACTIVATE). Drag it anywhere; the position is
remembered. Talks to the app through the same local API the main window uses.
"""
import json
import logging
import queue
import threading
import time
import urllib.request

log = logging.getLogger("rd.overlay")

try:
    import tkinter as tk
except Exception:          # Tk missing (shouldn't happen in the installed build)
    tk = None

BG = "#14161c"
PANEL = "#1d212a"
LINE = "#323846"
TEXT = "#e8eaef"
DIM = "#8b93a5"
RED = "#ff4655"
GREEN = "#3ddc97"
BLUE = "#3aa0ff"
FONT = "Segoe UI"


def _fmt(t):
    t = int(max(0, t or 0))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Overlay(threading.Thread):
    def __init__(self, base_url, load_settings, save_settings):
        super().__init__(daemon=True)
        self.base = base_url.rstrip("/")
        self.load_settings, self.save_settings = load_settings, save_settings
        self.cmds = queue.Queue()
        self.visible = False
        self.available = tk is not None
        self._status = {}

    # ---------------------------------------------------------- public (any thread)
    def toggle(self):
        self.cmds.put("toggle")

    def show(self):
        self.cmds.put("show")

    def hide(self):
        self.cmds.put("hide")

    def reset_position(self):
        """Move the panel back to the top-right corner of the main screen and show it."""
        self.cmds.put("reset")

    # ------------------------------------------------------------------ API calls
    def _post(self, path):
        def go():
            try:
                req = urllib.request.Request(self.base + path, data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                log.warning("overlay action %s failed: %s", path, e)
        threading.Thread(target=go, daemon=True).start()

    def _poll_loop(self):
        while True:
            if self.visible:
                try:
                    with urllib.request.urlopen(self.base + "/api/status?since=999999999", timeout=3) as r:
                        self._status = json.loads(r.read())
                except Exception:
                    self._status = {"_offline": True}
                time.sleep(0.4)
            else:
                time.sleep(0.5)

    # --------------------------------------------------------------------- UI
    def run(self):
        if not tk:
            log.warning("overlay unavailable: tkinter not installed")
            return
        threading.Thread(target=self._poll_loop, daemon=True).start()
        try:
            self._build()
            self.root.mainloop()
        except Exception:
            log.exception("overlay crashed")

    def _build(self):
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)            # no title bar
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        root.configure(bg=LINE)                # 1px border colour

        body = tk.Frame(root, bg=BG, padx=12, pady=10)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # header: status + close
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        self.dot = tk.Canvas(head, width=12, height=12, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 11, 11, fill=DIM, outline="")
        self.dot.pack(side="left", pady=(1, 0))
        self.status_lbl = tk.Label(head, text="Replay Desk", bg=BG, fg=TEXT, font=(FONT, 10, "bold"))
        self.status_lbl.pack(side="left", padx=(7, 0))
        close = tk.Label(head, text="✕", bg=BG, fg=DIM, font=(FONT, 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._hide())
        self.sub_lbl = tk.Label(body, text="", bg=BG, fg=DIM, font=(FONT, 8), anchor="w")
        self.sub_lbl.pack(fill="x", pady=(2, 8))

        # buttons
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        self.b_replay = self._button(row, "⟲  Save replay", GREEN, lambda: self._post("/api/replay/save"))
        self.b_record = self._button(row, "●  Record", RED, lambda: self._post("/api/record/toggle"))
        self.b_buffer = self._button(row, "Start buffer", GREEN, self._toggle_buffer)
        for i, b in enumerate((self.b_replay, self.b_record, self.b_buffer)):
            b.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0), sticky="nsew")
            row.grid_columnconfigure(i, weight=1)

        self.hint_lbl = tk.Label(body, text="", bg=BG, fg="#5d6577", font=(FONT, 8), anchor="w")
        self.hint_lbl.pack(fill="x", pady=(8, 0))

        # drag anywhere on the panel (except buttons)
        for w in (body, head, self.status_lbl, self.sub_lbl, self.hint_lbl, self.dot):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        root.update_idletasks()
        self._place()
        self._apply_win_styles()
        root.after(100, self._pump)
        root.after(300, self._refresh)

    def _button(self, parent, text, color, cmd):
        b = tk.Label(parent, text=text, bg=PANEL, fg=TEXT, font=(FONT, 9, "bold"), padx=10, pady=7,
                     cursor="hand2", highlightthickness=1, highlightbackground=LINE)
        b._color = color
        b.bind("<Button-1>", lambda e: (self._flash(b), cmd()))
        b.bind("<Enter>", lambda e: b.configure(bg="#262b36"))
        b.bind("<Leave>", lambda e: b.configure(bg=PANEL))
        return b

    def _flash(self, b):
        b.configure(bg="#2f3645")
        self.root.after(150, lambda: b.configure(bg=PANEL))

    def _toggle_buffer(self):
        on = self._status.get("buffer_running")
        self._post("/api/buffer/" + ("stop" if on else "start"))

    def _apply_win_styles(self):
        """Tool window (no taskbar button) that never steals focus from the game."""
        try:
            import ctypes
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = -20, 0x80, 0x08000000, 0x40000
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            get = user32.GetWindowLongPtrW if hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
            setl = user32.SetWindowLongPtrW if hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW
            get.restype = ctypes.c_ssize_t
            style = get(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
            setl(hwnd, GWL_EXSTYLE, ctypes.c_ssize_t(style))
        except Exception:
            pass   # not Windows

    # placement / dragging
    def _virtual_screen(self):
        """(left, top, right, bottom) of all monitors together, so a panel dragged to a
        second monitor is restored there. Falls back to the main screen."""
        try:
            import ctypes
            u = ctypes.windll.user32
            l, t = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            return l, t, l + u.GetSystemMetrics(78), t + u.GetSystemMetrics(79)
        except Exception:
            return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _place(self, reset=False):
        s = self.load_settings()
        w = self.root.winfo_reqwidth()
        sw = self.root.winfo_screenwidth()          # main screen (always starts at 0,0)
        x, y = s.get("overlay_x", -1), s.get("overlay_y", -1)
        l, t, r, b = self._virtual_screen()
        on_screen = x is not None and y is not None and l <= x <= r - 40 and t <= y <= b - 40 \
            and not (x == -1 and y == -1)
        if reset or not on_screen:
            x, y = sw - w - 24, 24                     # top-right corner of the main screen
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        try:
            self.save_settings({"overlay_x": self.root.winfo_x(), "overlay_y": self.root.winfo_y()})
        except Exception:
            pass

    # show / hide
    def _show(self):
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self._apply_win_styles()

    def _hide(self):
        self.visible = False
        self.root.withdraw()

    def _pump(self):
        try:
            while True:
                c = self.cmds.get_nowait()
                if c == "toggle":
                    self._hide() if self.visible else self._show()
                elif c == "show":
                    self._show()
                elif c == "hide":
                    self._hide()
                elif c == "reset":
                    self._place(reset=True)
                    self._show()
                    self.root.update()
                    self._drag_end(None)
        except queue.Empty:
            pass
        if self.visible:   # some games re-assert topmost; keep the panel above them
            self.root.attributes("-topmost", True)
        self.root.after(100, self._pump)

    def _refresh(self):
        s = self._status
        if self.visible and s:
            if s.get("_offline"):
                self.dot.itemconfigure(self.dot_id, fill=DIM)
                self.status_lbl.configure(text="Replay Desk isn't responding")
                self.sub_lbl.configure(text="")
            else:
                rec, buf = s.get("recording"), s.get("buffer_running")
                if rec:
                    color, title = RED, f"REC  {_fmt(s.get('recording_seconds'))}"
                elif buf:
                    color, title = GREEN, "Buffer on"
                else:
                    color, title = DIM, "Buffer off"
                if rec and int(time.time() * 2) % 2:   # blink while recording
                    color = "#7a2a31"
                self.dot.itemconfigure(self.dot_id, fill=color)
                self.status_lbl.configure(text=title, fg=RED if rec else TEXT)
                if buf:
                    sub = f"{_fmt(s.get('buffer_seconds'))} buffered · {s.get('fps', 0):.0f} fps"
                elif s.get("error"):
                    sub = "Capture stopped - open Replay Desk for details"
                else:
                    sub = "Start the buffer to save replays (Record starts it too)"
                self.sub_lbl.configure(text=sub)
                self.b_record.configure(text="■  Stop" if rec else "●  Record", fg=RED if rec else TEXT)
                self.b_buffer.configure(text="Stop buffer" if buf else "Start buffer", fg=DIM if buf else GREEN)
                self.b_replay.configure(fg=TEXT if buf else "#5d6577")
                st = self.load_settings()
                self.hint_lbl.configure(text=f"{st.get('hotkey_replay')} replay · {st.get('hotkey_record')} record · "
                                             f"{st.get('hotkey_overlay')} show/hide")
        self.root.after(250, self._refresh)
