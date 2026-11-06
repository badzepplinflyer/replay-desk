"""In-game controls ("overlay"), toggled with the overlay hotkey or the tray menu.

Two styles (Settings → Overlay style):
* side  - a full-height panel that slides in from the right edge of the main
          screen and slides away again on the next press (like GeForce Experience).
          Default; works over fullscreen-optimised, borderless and windowed games.
* float - a small floating panel you can drag anywhere.

Both stay above the game and never take focus from it (WS_EX_NOACTIVATE), so
clicking a button doesn't minimise or pause the game. Talks to the app through the
same local API the main window uses.
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
FAINT = "#5d6577"
RED = "#f0141c"
GREEN = "#3ddc97"
AMBER = "#ffb547"
FONT = "Segoe UI"
SIDE_WIDTH = 340          # logical pixels, scaled for DPI
SLIDE_MS = 170


def _fmt(t):
    t = int(max(0, t or 0))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Overlay(threading.Thread):
    def __init__(self, base_url, load_settings, save_settings, open_app=None):
        super().__init__(daemon=True)
        self.base = base_url.rstrip("/")
        self.load_settings, self.save_settings = load_settings, save_settings
        self.open_app = open_app
        self.cmds = queue.Queue()
        self.visible = False
        self.available = tk is not None
        self._status = {}
        self._style = None
        self._anim = None
        self._last_event = 0
        self._last_saved = None

    # ---------------------------------------------------------- public (any thread)
    def toggle(self):
        self.cmds.put("toggle")

    def show(self):
        self.cmds.put("show")

    def hide(self):
        self.cmds.put("hide")

    def reset_position(self):
        """Floating style: move back to the top-right of the main screen. Shows the overlay."""
        self.cmds.put("reset")

    # ------------------------------------------------------------------ API calls
    def _post(self, path, payload=None):
        body = json.dumps(payload or {}).encode()

        def go():
            try:
                req = urllib.request.Request(self.base + path, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                log.warning("overlay action %s failed: %s", path, e)
        threading.Thread(target=go, daemon=True).start()

    def _poll_loop(self):
        while True:
            if self.visible:
                try:
                    with urllib.request.urlopen(f"{self.base}/api/status?since={self._last_event}", timeout=3) as r:
                        st = json.loads(r.read())
                    for ev in st.get("events", []):
                        self._last_event = max(self._last_event, ev.get("id", 0))
                        if ev.get("kind") == "saved" and ev.get("path"):
                            self._last_saved = (ev["path"].replace("\\", "/").rsplit("/", 1)[-1], time.time())
                    self._status = st
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
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.overrideredirect(True)          # no title bar
            self.root.attributes("-topmost", True)
            self.root.configure(bg=LINE)
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            self._build(self._current_style())
            self.root.after(100, self._pump)
            self.root.after(300, self._refresh)
            self.root.mainloop()
        except Exception:
            log.exception("overlay crashed")

    def _current_style(self):
        st = (self.load_settings() or {}).get("overlay_style", "side")
        return st if st in ("side", "float") else "side"

    def _px(self, v):
        return int(round(v * self.scale))

    def _build(self, style):
        """(Re)create the panel contents for a style."""
        self._style = style
        for w in self.root.winfo_children():
            w.destroy()
        side = style == "side"
        self.root.attributes("-alpha", 0.96 if side else 0.94)
        body = tk.Frame(self.root, bg=BG, padx=self._px(22 if side else 12), pady=self._px(22 if side else 10))
        body.pack(fill="both", expand=True, padx=(1, 0) if side else 1, pady=0 if side else 1)
        self.body = body

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        if side:
            logo = self._logo_image(self._px(26))
            if logo is not None:
                tk.Label(head, image=logo, bg=BG).pack(side="left")
            else:
                tk.Label(head, text="Replay Desk", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        close = tk.Label(head, text="✕", bg=BG, fg=DIM, font=(FONT, 11 if side else 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._hide())

        page = body
        self.page_settings = None
        if side:
            self._tab = getattr(self, "_tab", "controls")
            tabs = tk.Frame(body, bg=BG)
            tabs.pack(fill="x", pady=(self._px(20), 0))
            self.tab_btns = {}
            for key, label in (("controls", "Controls"), ("settings", "Settings")):
                t = tk.Label(tabs, text=label, bg=BG, fg=DIM, font=(FONT, 10, "bold"), cursor="hand2",
                             padx=self._px(2), pady=self._px(6))
                t.pack(side="left", padx=(0, self._px(18)))
                t.bind("<Button-1>", lambda e, k=key: self._set_tab(k))
                self.tab_btns[key] = t
            self.tab_line = tk.Frame(body, bg=LINE, height=1)
            self.tab_line.pack(fill="x")
            page = self.page_main = tk.Frame(body, bg=BG)
            self.page_settings = tk.Frame(body, bg=BG)
            self._build_settings(self.page_settings)

        stat = tk.Frame(page, bg=BG)
        stat.pack(fill="x", pady=(self._px(22), 0) if side else (0, 0))
        if not side:
            stat.pack_forget()
            stat = head          # floating: status sits in the header row
        d = self._px(14 if side else 12)
        self.dot = tk.Canvas(stat, width=d, height=d, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, d - 1, d - 1, fill=DIM, outline="")
        self.dot.pack(side="left", pady=(self._px(2), 0))
        self.status_lbl = tk.Label(stat, text="Replay Desk", bg=BG, fg=TEXT,
                                   font=(FONT, 20 if side else 10, "bold"))
        self.status_lbl.pack(side="left", padx=(self._px(9 if side else 7), 0))
        self.sub_lbl = tk.Label(page, text="", bg=BG, fg=DIM, font=(FONT, 10 if side else 8), anchor="w",
                                justify="left", wraplength=self._px(SIDE_WIDTH - 50) if side else 0)
        self.sub_lbl.pack(fill="x", pady=(self._px(4), self._px(22 if side else 8)))

        btns = tk.Frame(page, bg=BG)
        btns.pack(fill="x")
        self.b_replay = self._button(btns, "⟲   Save replay", lambda: self._post("/api/replay/save"), side)
        self.b_record = self._button(btns, "●   Record", lambda: self._post("/api/record/toggle"), side)
        self.b_buffer = self._button(btns, "Start buffer", self._toggle_buffer, side)
        items = [self.b_replay, self.b_record, self.b_buffer]
        if side and self.open_app:
            self.b_open = self._button(btns, "Open Replay Desk", lambda: (self.open_app(), self._hide()), side)
            items.append(self.b_open)
        for i, b in enumerate(items):
            if side:
                b.pack(fill="x", pady=(0, self._px(10)))
            else:
                b.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0), sticky="nsew")
                btns.grid_columnconfigure(i, weight=1)

        if side:
            tk.Label(page, text="Last saved", bg=BG, fg=FAINT, font=(FONT, 9), anchor="w").pack(fill="x", pady=(self._px(18), 0))
            self.saved_lbl = tk.Label(page, text="Nothing saved yet this session", bg=BG, fg=TEXT,
                                      font=(FONT, 10), anchor="w", justify="left", wraplength=self._px(SIDE_WIDTH - 50))
            self.saved_lbl.pack(fill="x", pady=(self._px(2), 0))
        else:
            self.saved_lbl = None
        self.hint_lbl = tk.Label(body, text="", bg=BG, fg=FAINT, font=(FONT, 9 if side else 8), anchor="w", justify="left",
                                 wraplength=self._px(SIDE_WIDTH - 50) if side else 0)
        self.hint_lbl.pack(side="bottom" if side else "top", fill="x", pady=(self._px(8), 0))
        if side:
            self._set_tab(self._tab)

        if not side:   # drag anywhere on the floating panel (except buttons)
            for w in (body, head, self.status_lbl, self.sub_lbl, self.hint_lbl, self.dot):
                w.bind("<ButtonPress-1>", self._drag_start)
                w.bind("<B1-Motion>", self._drag_move)
                w.bind("<ButtonRelease-1>", self._drag_end)

        self.root.update_idletasks()
        self._place()
        self._apply_win_styles()

    # ------------------------------------------------------------ settings tab
    SETTING_ROWS = [
        ("buffer_seconds", "Replay length", [(30, "30s"), (60, "1m"), (120, "2m"), (300, "5m"), (600, "10m")]),
        ("quality", "Quality", [("low", "Low"), ("medium", "Medium"), ("high", "High")]),
        ("fps", "Frame rate", [(30, "30"), (60, "60"), (120, "120")]),
        ("game_audio", "Game audio", [(True, "On"), (False, "Off")]),
        ("mic_enabled", "Microphone", [(True, "On"), (False, "Off")]),
        ("sound_feedback", "Sound effects", [(True, "On"), (False, "Off")]),
        ("start_buffer_on_launch", "Buffer at startup", [(True, "On"), (False, "Off")]),
    ]
    CAPTURE_KEYS = ("buffer_seconds", "quality", "fps", "game_audio", "mic_enabled")

    def _build_settings(self, page):
        cur = self.load_settings() or {}
        self._seg = {}
        tk.Frame(page, bg=BG, height=self._px(14)).pack(fill="x")
        for key, label, opts in self.SETTING_ROWS:
            row = tk.Frame(page, bg=BG)
            row.pack(fill="x", pady=(0, self._px(12)))
            tk.Label(row, text=label, bg=BG, fg=DIM, font=(FONT, 9), anchor="w").pack(fill="x")
            seg = tk.Frame(row, bg=LINE)
            seg.pack(fill="x", pady=(self._px(4), 0))
            cells = []
            for i, (val, text) in enumerate(opts):
                c = tk.Label(seg, text=text, bg=PANEL, fg=TEXT, font=(FONT, 9, "bold"), cursor="hand2",
                             pady=self._px(7))
                c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0), pady=1)
                seg.grid_columnconfigure(i, weight=1, uniform=key)
                c.bind("<Button-1>", lambda e, k=key, v=val: self._set_setting(k, v))
                cells.append((val, c))
            self._seg[key] = cells
            self._paint_seg(key, cur.get(key))

        tk.Label(page, text="Hotkeys", bg=BG, fg=DIM, font=(FONT, 9), anchor="w").pack(fill="x", pady=(self._px(4), 0))
        hk = tk.Frame(page, bg=BG)
        hk.pack(fill="x", pady=(self._px(2), self._px(12)))
        self.hk_vals = {}
        for i, (k, name) in enumerate((("hotkey_replay", "Save replay"), ("hotkey_record", "Record"),
                                       ("hotkey_overlay", "Overlay"))):
            tk.Label(hk, text=name, bg=BG, fg=TEXT, font=(FONT, 9), anchor="w").grid(row=i, column=0, sticky="w")
            v = tk.Label(hk, text="", bg=BG, fg=DIM, font=(FONT, 9), anchor="w")
            v.grid(row=i, column=1, sticky="w", padx=(self._px(16), 0))
            self.hk_vals[k] = v
        self._paint_hotkeys(cur)

        links = tk.Frame(page, bg=BG)
        links.pack(fill="x")
        self._button(links, "Open saved clips folder", self._open_output, True).pack(fill="x", pady=(0, self._px(8)))
        if self.open_app:
            self._button(links, "All settings & hotkeys…", lambda: (self.open_app(), self._hide()), True).pack(fill="x")

    def _paint_seg(self, key, value):
        for val, c in self._seg.get(key, []):
            on = val == value
            c.configure(bg=RED if on else PANEL, fg="#ffffff" if on else DIM)

    def _paint_hotkeys(self, cur):
        for k, v in self.hk_vals.items():
            v.configure(text=cur.get(k, ""))

    def _set_setting(self, key, value):
        self._paint_seg(key, value)
        self._post("/api/settings", {key: value})
        if key in self.CAPTURE_KEYS and self._status.get("buffer_running"):
            self._note("Saved. The buffer restarts for a moment to apply it.")
        else:
            self._note("Saved.")

    def _note(self, text):
        self.hint_lbl.configure(text=text, fg=GREEN)
        self._note_until = time.time() + 3

    def _open_output(self):
        try:
            import os
            os.startfile((self.load_settings() or {}).get("output_dir", ""))
        except Exception as e:
            log.warning("open folder failed: %s", e)

    def _set_tab(self, key):
        self._tab = key
        for k, t in self.tab_btns.items():
            t.configure(fg=TEXT if k == key else FAINT,
                        highlightthickness=0, borderwidth=0)
        if key == "settings":
            self.page_main.pack_forget()
            cur = self.load_settings() or {}       # pick up changes made in the main window
            for k in self._seg:
                self._paint_seg(k, cur.get(k))
            self._paint_hotkeys(cur)
            self.page_settings.pack(fill="both", expand=True)
        else:
            self.page_settings.pack_forget()
            self.page_main.pack(fill="both", expand=True)

    def _logo_image(self, height):
        """The Replay Desk logo (horizontal version) sized for the panel header."""
        try:
            from . import config
            from PIL import Image, ImageTk
            im = Image.open(config.APP_DIR / "web" / "img" / "lockup.png").convert("RGBA")
            im = im.resize((int(im.width * height / im.height), height), Image.LANCZOS)
            self._logo_ref = ImageTk.PhotoImage(im, master=self.root)   # keep a reference
            return self._logo_ref
        except Exception:
            return None

    def _button(self, parent, text, cmd, side):
        b = tk.Label(parent, text=text, bg=PANEL, fg=TEXT, font=(FONT, 11 if side else 9, "bold"),
                     padx=self._px(14 if side else 10), pady=self._px(12 if side else 7), anchor="w" if side else "center",
                     cursor="hand2", highlightthickness=1, highlightbackground=LINE)
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

    # ------------------------------------------------------------ placement
    def _virtual_screen(self):
        try:
            import ctypes
            u = ctypes.windll.user32
            l, t = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            return l, t, l + u.GetSystemMetrics(78), t + u.GetSystemMetrics(79)
        except Exception:
            return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _side_geom(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = self._px(SIDE_WIDTH)
        return sw, sh, w

    def _place(self, reset=False):
        if self._style == "side":
            sw, sh, w = self._side_geom()
            x = sw - w if self.visible else sw            # parked just off the right edge
            self.root.geometry(f"{w}x{sh}+{x}+0")
            return
        self.root.geometry("")                           # floating: natural size
        self.root.update_idletasks()
        s = self.load_settings()
        w = self.root.winfo_reqwidth()
        sw = self.root.winfo_screenwidth()
        x, y = s.get("overlay_x", -1), s.get("overlay_y", -1)
        l, t, r, b = self._virtual_screen()
        on_screen = x is not None and y is not None and l <= x <= r - 40 and t <= y <= b - 40 \
            and not (x == -1 and y == -1)
        if reset or not on_screen:
            x, y = sw - w - 24, 24
        self.root.geometry(f"+{int(x)}+{int(y)}")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        if self._style != "float":
            return
        try:
            self.save_settings({"overlay_x": self.root.winfo_x(), "overlay_y": self.root.winfo_y()})
        except Exception:
            pass

    # ------------------------------------------------------------ show / hide
    def _slide(self, start, end, done=None):
        if self._anim:
            self.root.after_cancel(self._anim)
        sw, sh, w = self._side_geom()
        t0 = time.perf_counter()

        def step():
            p = min(1.0, (time.perf_counter() - t0) * 1000 / SLIDE_MS)
            ease = 1 - (1 - p) ** 3
            x = int(start + (end - start) * ease)
            self.root.geometry(f"{w}x{sh}+{x}+0")
            if p < 1:
                self._anim = self.root.after(8, step)
            else:
                self._anim = None
                if done:
                    done()
        step()

    def _show(self):
        style = self._current_style()
        if style != self._style:
            self._build(style)
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self._apply_win_styles()
        if self._style == "side":
            sw, sh, w = self._side_geom()
            self._slide(sw, sw - w)
        else:
            self._place()

    def _hide(self):
        if not self.visible:
            return
        self.visible = False
        if self._style == "side":
            sw, sh, w = self._side_geom()
            self._slide(self.root.winfo_x(), sw, done=lambda: (not self.visible) and self.root.withdraw())
        else:
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
                    if self._current_style() == "float":
                        self.save_settings({"overlay_x": -1, "overlay_y": -1})
                    if self._style != self._current_style():
                        self._build(self._current_style())
                    if self.visible and self._style == "float":
                        self._place(reset=True)
                    else:
                        self._show()
        except queue.Empty:
            pass
        if self.visible:   # some games re-assert topmost; keep the panel above them
            self.root.attributes("-topmost", True)
        self.root.after(100, self._pump)

    # ------------------------------------------------------------ live status
    def _refresh(self):
        s = self._status
        try:
            if self.visible and s:
                self._render(s)
        except Exception:
            log.exception("overlay refresh failed")
        self.root.after(250, self._refresh)

    def _render(self, s):
        side = self._style == "side"
        if s.get("_offline"):
            self.dot.itemconfigure(self.dot_id, fill=DIM)
            self.status_lbl.configure(text="Not responding" if side else "Replay Desk isn't responding", fg=TEXT)
            self.sub_lbl.configure(text="Replay Desk may have closed. Open it from the Start menu.")
            return
        lic = s.get("license") or {}
        if lic.get("allowed") is False:
            self.dot.itemconfigure(self.dot_id, fill=AMBER)
            self.status_lbl.configure(text="Trial ended" if side else "Free trial ended", fg=TEXT)
            self.sub_lbl.configure(text="Open Replay Desk to unlock it for life.")
            return
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
            sub = "Capture stopped. Open Replay Desk for details."
        else:
            sub = "Start the buffer to save replays (Record starts it too)."
        self.sub_lbl.configure(text=sub)
        self.b_record.configure(text=("■   Stop recording" if side else "■  Stop") if rec else ("●   Record" if side else "●  Record"),
                                fg=RED if rec else TEXT)
        self.b_buffer.configure(text="Stop buffer" if buf else "Start buffer", fg=DIM if buf else GREEN)
        self.b_replay.configure(fg=TEXT if buf else FAINT)
        if self.saved_lbl is not None and self._last_saved:
            name, t = self._last_saved
            ago = int(time.time() - t)
            when = "just now" if ago < 60 else f"{ago // 60} min ago"
            self.saved_lbl.configure(text=f"{name}\n{when}")
        st = self.load_settings()
        if time.time() < getattr(self, "_note_until", 0):
            return                      # keep the "Saved." note up for a moment
        if side:
            self.hint_lbl.configure(fg=FAINT, text=f"Press {st.get('hotkey_overlay')} to hide this panel.\n"
                                         f"Save replay: {st.get('hotkey_replay')}   Record: {st.get('hotkey_record')}")
        else:
            self.hint_lbl.configure(text=f"Replay {st.get('hotkey_replay')}   Record {st.get('hotkey_record')}\n"
                                         f"{st.get('hotkey_overlay')} shows or hides this panel")
