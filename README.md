# Replay Desk

Replay Desk records your games, keeps an instant replay buffer, and lets you cut recordings down to clips. It runs on Windows with an NVIDIA GPU.

## Download

**[Download Replay Desk for Windows](https://github.com/badzepplinflyer/replay-desk/releases/latest/download/ReplayDesk-Setup.exe)**

Run `ReplayDesk-Setup.exe`. It installs just for your Windows account, so no admin password is needed, and it adds a desktop shortcut. Everything the app needs, including FFmpeg, is built in.

If Windows shows **"Windows protected your PC"**, click **More info → Run anyway**. That warning appears because the app isn't code-signed, and you'll only see it once.

Requirements: Windows 10 or 11, 64-bit. An NVIDIA GPU is recommended; AMD cards and CPU encoding work too (change the encoder in Settings).

### Updates

Replay Desk checks for new versions automatically. When one is available, a banner shows what's new, and **Update now** installs it and reopens the app. If you'd rather not think about it, turn on **Settings → Install updates automatically**: new versions download in the background and install the next time the app opens. Your settings and videos are always kept.

## Using it

When Replay Desk opens, the replay buffer starts and the window shows a green **Buffer on**. If you close the window, the app keeps running in the system tray (the red icon near the clock), so your hotkeys still work in-game. To fully exit, right-click the tray icon and choose **Quit**.

| Hotkey | What it does |
|---|---|
| **Ctrl+Alt+F10** | Saves the last 5 minutes (you can change the length) to `Videos\Replay Desk\Replays` |
| **Ctrl+Alt+F9** | Starts or stops a full recording, saved to `Videos\Replay Desk\Recordings` |

You'll hear two quick beeps when a replay saves. Files are named after the game, for example `iRacing_2026-10-31_20-15-02.mp4`. You can change either hotkey on the Capture tab: click the box and press the new key combination.

### Overlay

Press **Ctrl+Alt+O** while you play to pop up a small control panel on top of the game, with **Save replay**, **Record / Stop** and **Start / Stop buffer** buttons, plus live status and the recording timer. Clicking it doesn't take focus from your game. Drag it wherever you like and it remembers the spot. Press the hotkey again, or click ✕, to hide it. You can also open it from the **⧉ Overlay** button in the app or from the tray menu. The overlay shows over games in borderless or windowed mode, not exclusive fullscreen.

### Making a clip

1. Open **Library** and click a replay or recording.
2. Move the playhead to where the clip should start and press **I**. Move to where it should end and press **O**.
3. Press **A** to add that part as a segment. Repeat to keep more parts. They're joined in the order shown in the Segments list, and you can reorder them with ▲▼.
4. Press **P** to preview only the parts you kept.
5. Set the Game and Mic levels. Drag the Mic slider to 0 to remove your voice.
6. Click **Export clip**. The clip is saved to `Videos\Replay Desk\Clips`.

Editor keys: Space plays and pauses · ← → jump 1 s (hold Shift for 5 s) · `,` `.` step one frame · S splits the segment at the playhead · Del removes the selected segment · Ctrl+scroll (or the Zoom slider) zooms the timeline. You can drag the edges of any segment, or of the white In/Out box, to fine-tune.

### Compiling clips into one video

1. In **Library**, click **Compile clips**.
2. Click videos in the order you want them to play. Each one gets a number, and clicking it again removes it. You can mix clips, replays and recordings.
3. Click **Build compilation →**.
4. On the **Compile** tab, drag clips to reorder them, or use ▲▼. Press ▶ to preview the clips back to back.
5. Choose a transition: **Cut**, **Fade to black** or **Crossfade**, and its length.
6. Click **Export compilation**. The video is saved to `Videos\Replay Desk\Compilations`.

Clips with different sizes or frame rates are matched automatically. Smaller clips get black bars, and clips without sound get silence.

## Audio tracks

Every recording has three audio tracks:
1. **Game + Mic** — what you hear when you play the file anywhere
2. **Game** only
3. **Mic** only

The editor uses tracks 2 and 3, so in an exported clip you can turn your mic up, down, or off without touching the game sound.

## Tips and troubleshooting

- **Use borderless or windowed fullscreen** in your games. Capture works best that way. iRacing and MSFS 2024 both have the option.
- **The hotkey does nothing / Capture tab says "in use by another app".** Another program already owns that key, usually the NVIDIA overlay (Alt+F10 and Alt+F9 are its defaults). Pick a different combination.
- **Audio is slightly ahead of or behind the video.** On the Capture tab, adjust **Audio sync offset**. A positive value delays the audio.
- **A game switches resolution when it starts.** Capture restarts itself within about a second, which leaves a short gap in the buffer.
- **The capture box shows an error.** The log is at `%LOCALAPPDATA%\ReplayDesk\replaydesk.log`.
- **HDR**: if colors look washed out in recordings, turn off Windows HDR while playing.
- **Disk use**: the replay buffer keeps about 1–3 GB of temporary files in `%LOCALAPPDATA%\ReplayDesk\buffer`. They're cleared every time the app starts.
- **Start with Windows**: run the installer again and tick "Start Replay Desk in the tray when Windows starts".

## For the developer: publishing a new version

1. Make changes, and add a `## x.y.z` section to `CHANGELOG.md`. That text becomes the "What's new" notes in the app.
2. Set `__version__ = "x.y.z"` in `rd/version.py`, then commit and push to `main`.
3. GitHub Actions builds `ReplayDesk-Setup.exe` and publishes the release (about 5–8 minutes). Installed apps pick it up within a few hours, or right away with **Check for updates**.

To run from source for development: `pip install -r requirements.txt`, then `python app.py`. FFmpeg must be on your PATH or in `ffmpeg\`.
