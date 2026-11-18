#!/usr/bin/env python3
"""
Video Compressor - a cross-platform (Windows / macOS / Linux) tool that shrinks
a video to fit a file-size limit, e.g. Discord's upload limit or an email attachment.

Requirements:
    Python 3.8+ (with Tkinter, included in the python.org installers)
    imageio-ffmpeg -> bundles an FFmpeg binary (no separate install needed)
    tkinterdnd2    -> drag and drop (optional: the app works without it)
    The app offers to install them on first run. Manual install, using the Python that runs the app:
        python -m pip install imageio-ffmpeg tkinterdnd2

Run:
    python video_compressor.py
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.wmv *.flv *.mpg *.mpeg *.3gp"),
    ("All files", "*.*"),
]

# Size presets in MB. "MB" here is 1,000,000 bytes, the smaller of the two
# meanings, so the file fits whichever one a service actually uses.
# Limits change; these were checked in November 2026.
PRESETS = {
    "Discord (free) — 20 MB": 20,
    "Discord Nitro Basic — 50 MB": 50,
    "Email (Gmail, Outlook) — 18 MB": 18,  # attachments grow ~35% when emailed; 18 MB fits a 25 MB limit
    "Custom": None,
}
MB = 1_000_000
SIZE_SAFETY = 0.96   # aim 4% under the target for MP4 overhead and encoder error
MAX_PASS2_RETRIES = 2
MIN_VIDEO_BPS = 100_000  # below this H.264 is a smeared mess at any size
SOURCE_CAP = 0.9  # never use more than 90% of the source's own bitrate

RESOLUTIONS = ["Auto", "Original", "1080p", "720p", "480p", "360p", "240p"]
AUTO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240]
FRAME_RATES = ["Auto", "Original", "30", "24"]
# Bits per pixel per frame that H.264 (preset medium) needs to look decent.
# Auto picks the biggest picture that gets at least this much.
GOOD_BPP = 0.04
POOR_BPP = 0.02  # below this, warn that quality will be low


# --------------------------------------------------------------------------- #
#  FFmpeg helpers
# --------------------------------------------------------------------------- #
# Hide the console window that would otherwise flash up on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def ffmpeg_check(path):
    """Return (usable, version_string). 'Usable' means it can encode H.264 + AAC.

    Old copies bundled with other software often can't (or can't run at all).
    """
    try:
        ver = subprocess.run([path, "-version"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
        enc = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             errors="replace", timeout=15, **_POPEN_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return False, "couldn't run"
    m = re.search(r"version\s+(\S+)", ver)
    return ("libx264" in enc and re.search(r"\baac\b", enc) is not None), (m[1] if m else "unknown")


def find_ffmpeg():
    """Return (path, description) of a usable FFmpeg, or (None, reason).

    Prefers the copy bundled by imageio-ffmpeg (known-good, recent), and only
    accepts one from PATH if it can make H.264 MP4s.
    """
    try:
        import importlib
        importlib.invalidate_caches()  # see packages installed since startup
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} (bundled)"
    except Exception:
        pass
    path = shutil.which("ffmpeg")
    if path:
        ok, ver = ffmpeg_check(path)
        if ok:
            return path, f"FFmpeg {ver} ({path})"
        return None, f"The FFmpeg found at {path} (version {ver}) is too old or can't make MP4s."
    return None, "FFmpeg isn't installed."


def install_requirements():
    """pip-install the dependency into *this* Python interpreter.

    Using sys.executable guarantees it lands in the same Python that's running
    the app — the usual cause of 'I installed it but it's not found' is having
    several Pythons, where `pip` and double-clicking the script use different ones.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pythonw has no console; use python.exe for pip
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([exe, "-m", "pip", "install", "--upgrade", "imageio-ffmpeg", "tkinterdnd2"],
                       capture_output=True, text=True, errors="replace", **_POPEN_FLAGS)
    add_user_site()
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-3:]


def add_user_site():
    """Make packages pip just put in the per-user folder importable right away.

    When Python's own folder isn't writable (e.g. installed under Program
    Files), pip installs to the user folder instead (%APPDATA%\\Python\\...
    on Windows, ~/Library/Python/... on macOS). Python only adds that folder
    to sys.path at startup if it already existed, so on a first install it
    isn't searched until the app is restarted.
    """
    import site
    path = site.getusersitepackages() if site.ENABLE_USER_SITE else None  # off in venvs
    if path and os.path.isdir(path) and path not in sys.path:
        site.addsitedir(path)


def probe_video(ffmpeg, path):
    """Return a dict (duration, width, height, fps, has_audio, error) by parsing `ffmpeg -i`.

    This only reads the container header, so it's instant even for huge files.
    width/height are as DISPLAYED: phone videos are often stored landscape with
    a 'rotate 90°' flag, and FFmpeg turns them upright when encoding.
    """
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                         capture_output=True, text=True, errors="replace",
                         **_POPEN_FLAGS).stderr
    info = dict(duration=None, width=None, height=None, fps=None, has_audio=False, bitrate=None,
                error=None)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Duration:.*?bitrate:\s*(\d+)\s*kb/s", out)  # whole file: video + sound
    if m:
        info["bitrate"] = int(m[1]) * 1000
    m = re.search(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        info["width"], info["height"] = int(m[1]), int(m[2])
    m = re.search(r"Stream #.*Video:.*?(\d+(?:\.\d+)?)\s*fps", out)
    if m:
        info["fps"] = float(m[1])
    m = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", out) or re.search(r"rotate\s*:\s*(-?\d+)", out)
    if m and info["width"] and round(abs(float(m[1]))) % 180 == 90:
        info["width"], info["height"] = info["height"], info["width"]
    info["has_audio"] = bool(re.search(r"Stream #.*Audio:", out))
    if not (info["duration"] and info["width"]):
        info["error"] = explain_probe_error(out)
    return info


def explain_probe_error(ffmpeg_output):
    """Turn FFmpeg's error output into a short, human-readable reason."""
    text = ffmpeg_output.lower()
    if "moov atom not found" in text or "invalid data found" in text:
        return ("The file looks incomplete or damaged — common if the recording was "
                "interrupted or is still being written (e.g. OBS still recording).")
    if "permission denied" in text:
        return "Permission denied — the file may be open in another program."
    if "no such file" in text:
        return "File not found."
    if "does not contain any stream" in text or "video:" not in text:
        return "No video found in this file."
    lines = [l.strip() for l in ffmpeg_output.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "FFmpeg returned no information."


# ---- planning: how big a picture fits in the size limit ------------------- #
def audio_bitrate(total_bps):
    """Spend less on sound when the budget is tight; speech stays clear at 64k AAC."""
    if total_bps < 600_000:
        return 64_000
    if total_bps < 1_500_000:
        return 96_000
    return 128_000


def scaled_size(src_w, src_h, short_side):
    """Size with the SHORT side set to `short_side`, so '720p' means the same for
    landscape (1280×720) and portrait (720×1280) videos. Even numbers, because
    H.264 with 4:2:0 color can't encode odd dimensions. Never upscales."""
    if short_side >= min(src_w, src_h):
        return src_w // 2 * 2, src_h // 2 * 2
    scale = short_side / min(src_w, src_h)
    return round(src_w * scale / 2) * 2, round(src_h * scale / 2) * 2


def plan_encode(target_mb, duration, src_w, src_h, src_fps, has_audio, keep_audio,
                res_choice="Auto", fps_choice="Auto", src_bps=None):
    """Work out the bitrates, picture size and frame rate for a target file size.

    File size of a video is basically bitrate × duration, so we can compute the
    bitrate exactly instead of guessing. The target is a ceiling, not a goal:
    we never use more bitrate than the source has (`src_bps`), because that only
    makes the file bigger without looking any better. Returns a dict, or raises
    ValueError with a friendly message when the target is too small for the clip.
    """
    budget_bps = target_mb * MB * 8 * SIZE_SAFETY / duration
    audio_bps = audio_bitrate(budget_bps) if (has_audio and keep_audio) else 0
    video_bps = int(budget_bps - audio_bps)
    # 90%: re-encoding at the source's exact bitrate can come out a little BIGGER
    # than the original; a bit under keeps "compress" true at almost no visible cost.
    ceiling = src_bps * SOURCE_CAP - audio_bps if src_bps else None
    capped = bool(ceiling) and video_bps > ceiling > MIN_VIDEO_BPS
    if capped:
        video_bps = int(ceiling)
        # Plenty of room: Auto keeps the source's own size and frame rate,
        # since shrinking them would only lose detail for no saving.
        res_choice = "Original" if res_choice == "Auto" else res_choice
        fps_choice = "Original" if fps_choice == "Auto" else fps_choice
    if video_bps < MIN_VIDEO_BPS:
        need_mb = (MIN_VIDEO_BPS + audio_bps) * duration / 8 / SIZE_SAFETY / MB
        raise ValueError(
            f"{target_mb:g} MB is too small for {duration:.0f} s of video.\n\n"
            f"It needs at least {need_mb:.1f} MB. Use a shorter part of the video"
            + (", turn off sound," if audio_bps else "") + " or raise the target size.")
    src_fps = src_fps or 30

    def fps_for(choice):
        if choice == "Original" or choice == "Auto":
            return src_fps
        return min(src_fps, float(choice))  # never invent frames

    def bpp(w, h, fps):
        return video_bps / (w * h * fps)

    if res_choice == "Auto" or fps_choice == "Auto":
        # Try the biggest picture first, at the source frame rate and then at 30 fps,
        # and take the first one that gets enough bits per pixel to look decent.
        heights = ([min(src_w, src_h)] + [h for h in AUTO_HEIGHTS if h < min(src_w, src_h)]
                   if res_choice == "Auto" else [None])
        fpss = ([src_fps] + ([30.0] if src_fps > 30 else [])) if fps_choice == "Auto" else [fps_for(fps_choice)]
        choice = None
        for h in heights:
            w2, h2 = (scaled_size(src_w, src_h, h) if h else
                      fixed_size(src_w, src_h, res_choice))
            for f in fpss:
                if bpp(w2, h2, f) >= GOOD_BPP:
                    choice = (w2, h2, f)
                    break
            if choice:
                break
        if not choice:  # even the smallest isn't 'good': use the smallest
            w2, h2 = (scaled_size(src_w, src_h, heights[-1]) if heights[-1] else
                      fixed_size(src_w, src_h, res_choice))
            choice = (w2, h2, fpss[-1])
        width, height, fps = choice
    else:
        width, height = fixed_size(src_w, src_h, res_choice)
        fps = fps_for(fps_choice)

    return dict(video_bps=video_bps, audio_bps=audio_bps, width=width, height=height,
                fps=fps, change_fps=abs(fps - src_fps) > 0.01,
                bpp=bpp(width, height, fps), capped=capped)


def fixed_size(src_w, src_h, res_choice):
    if res_choice in ("Original", "Auto"):
        return src_w // 2 * 2, src_h // 2 * 2
    return scaled_size(src_w, src_h, int(res_choice.rstrip("p")))


def build_pass_command(ffmpeg, o, plan, pass_no, passlog, video_bps):
    """One pass of a two-pass H.264 encode.

    Two-pass: pass 1 only analyzes the video (which scenes are complex), and
    pass 2 uses that to spread the bitrate so the file lands on the target size
    with the best quality for that size. A single pass has to guess ahead.
    """
    cmd = [ffmpeg, "-y", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1"]
    if o["start"]:
        cmd += ["-ss", f"{o['start']:.3f}"]  # before -i = fast seek
    cmd += ["-t", f"{o['duration']:.3f}", "-i", o["src"], "-map", "0:v:0"]
    filters = []
    if plan["change_fps"]:
        filters.append(f"fps={plan['fps']:g}")
    if (plan["width"], plan["height"]) != (o["src_w"], o["src_h"]):
        filters.append(f"scale={plan['width']}:{plan['height']}:flags=lanczos")
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-b:v", str(video_bps),
            "-pix_fmt", "yuv420p", "-pass", str(pass_no), "-passlogfile", passlog]
    if pass_no == 1:
        return cmd + ["-an", "-f", "null", os.devnull]
    if plan["audio_bps"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", str(plan["audio_bps"])]
    else:
        cmd += ["-an"]
    # faststart moves the index to the front so it plays before fully downloaded
    return cmd + ["-movflags", "+faststart", o["dst"]]


# ---- small formatting helpers ---------------------------------------------- #
def parse_time(text):
    """Accept '75', '75.5', '1:15', or '0:01:15.5' and return seconds."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Bad time: {text!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def fmt_time(secs):
    m, s = divmod(secs, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def fmt_size(n):
    """Bytes as MB (1,000,000 bytes), the same unit as the target size."""
    return f"{n / MB:.2f} MB" if n >= MB else f"{n / 1000:.0f} KB"


class EncodeError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class CompressorApp:
    def __init__(self, root):
        self.root = root
        root.title("Video Compressor")
        root.minsize(560, 0)
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        self.proc = None
        self.cancelled = False
        self.info = None  # probe_video() result for the chosen video
        self.info_path = None  # ...and which file that was
        self.ui_queue = queue.Queue()
        self._pump_ui()
        # Show unexpected errors in a dialog. By default Tk prints them to a
        # console, which is invisible when the script is double-clicked.
        root.report_callback_exception = self._report_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        home = os.path.expanduser("~")
        desktop = os.path.join(home, "Desktop")
        first = next(iter(PRESETS))
        self.v = {
            "src": tk.StringVar(),
            "preset": tk.StringVar(value=first),
            "target": tk.StringVar(value=f"{PRESETS[first]:g}"),
            "start": tk.StringVar(),
            "end": tk.StringVar(),
            "res": tk.StringVar(value="Auto"),
            "fps": tk.StringVar(value="Auto"),
            "audio": tk.BooleanVar(value=True),
            "out_dir": tk.StringVar(value=desktop if os.path.isdir(desktop) else home),
            "out_name": tk.StringVar(),
            "reveal": tk.BooleanVar(value=True),
        }
        self._build_ui()
        self.v["preset"].trace_add("write", lambda *_: self._on_preset())
        for key in ("target", "start", "end", "res", "fps", "audio"):
            self.v[key].trace_add("write", lambda *_: self._update_plan_label())
        self._on_preset()
        self.dnd = enable_drag_and_drop(root, self._on_drop)
        if self.dnd:
            self.info_lbl.config(text="No video selected — drag one here, or use Browse.")

        if self.ffmpeg:
            self.status.config(text=f"Ready — using {self.ffmpeg_desc}.")
        else:
            # Defer so the main window is drawn before the dialog appears.
            self.root.after(200, self.offer_install)

    # ---- dependency installer ------------------------------------------ #
    def offer_install(self):
        if getattr(sys, "frozen", False):
            # The downloadable app bundles FFmpeg, and sys.executable is the app
            # itself, so "pip install" would just launch another copy of it.
            messagebox.showerror("Missing FFmpeg", f"{self.ffmpeg_desc}\n\n"
                                 "This copy of Video Compressor is incomplete. Please download it again.")
            return
        if not messagebox.askyesno(
                "Install FFmpeg?",
                f"Video Compressor needs FFmpeg — {self.ffmpeg_desc}\n\n"
                "Install it now? (about 30 MB download)\n\n"
                f"This runs pip for the Python running this app:\n{sys.executable}"):
            self.status.config(text="FFmpeg missing — nothing can be compressed until it's installed.")
            return
        self._set_busy(True)
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Installing FFmpeg… this can take a minute.")

        def work():
            try:
                ok, tail = install_requirements()
            except Exception as e:
                ok, tail = False, [str(e)]
            self.ui(lambda: self._install_done(ok, tail))
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, ok, tail):
        self._set_busy(False)
        self.ffmpeg, self.ffmpeg_desc = find_ffmpeg()
        if ok and self.ffmpeg:
            self.status.config(text=f"Installed! Using {self.ffmpeg_desc}.")
            if self.v["src"].get():
                self._probe(self.v["src"].get())  # re-read the already chosen video
            if not self.dnd:  # tkinterdnd2 was just installed too
                self.dnd = enable_drag_and_drop(self.root, self._on_drop)
        else:
            self.status.config(text="Install failed — see the error message.")
            messagebox.showerror(
                "Install failed",
                "Couldn't install automatically:\n\n" + "\n".join(tail) +
                "\n\nTry running this in a terminal:\n"
                f'"{sys.executable}" -m pip install imageio-ffmpeg tkinterdnd2')

    # ---- layout --------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        app_header(self.root, "Video Compressor", "Fit any video under a size limit · free, private, no limits", "MB").pack(fill="x")
        main = ttk.Frame(self.root, padding=(12, 8), style="App.TFrame")
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        f = section(main, "1. Video", "play")
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.v["src"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="Browse…", command=self.pick_video).grid(row=0, column=1, padx=(6, 0))
        self.info_lbl = ttk.Label(f, text="No video selected", foreground=MUTED, wraplength=500)
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = section(main, "2. Target size", "target")
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Combobox(f, values=list(PRESETS), textvariable=self.v["preset"],
                     state="readonly", width=30).grid(row=0, column=0, sticky="w", **pad)
        self.target_e = ttk.Entry(f, textvariable=self.v["target"], width=8)
        self.target_e.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="MB").grid(row=0, column=2, sticky="w")
        ttk.Label(f, text="Upload limits change over time. Pick Custom to type any size.",
                  foreground=MUTED).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        f = section(main, "3. Part of the video (optional)", "scissors")
        f.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Start").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["start"], width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="End").grid(row=0, column=2, sticky="w")
        ttk.Entry(f, textvariable=self.v["end"], width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(f, text="Blank = whole video. Seconds (12.5) or m:ss (1:05). "
                          "A shorter part gets better quality at the same size.",
                  foreground=MUTED, wraplength=500).grid(row=1, column=0, columnspan=4, sticky="w")

        f = section(main, "4. Quality", "settings")
        f.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Resolution").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=RESOLUTIONS, textvariable=self.v["res"],
                     state="readonly", width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Frame rate").grid(row=0, column=2, sticky="w", **pad)
        ttk.Combobox(f, values=FRAME_RATES, textvariable=self.v["fps"],
                     state="readonly", width=10).grid(row=0, column=3, sticky="w", **pad)
        self.audio_w = ttk.Checkbutton(f, text="Keep sound", variable=self.v["audio"])
        self.audio_w.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.plan_lbl = ttk.Label(f, text="", foreground=MUTED, wraplength=500, justify="left")
        self.plan_lbl.grid(row=2, column=0, columnspan=4, sticky="w", **pad)

        f = section(main, "5. Save to", "download")
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["out_dir"]).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Choose…", command=self.pick_folder).grid(row=0, column=2)
        ttk.Label(f, text="File name").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=self.v["out_name"]).grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Checkbutton(f, text="Show the file in its folder when done",
                        variable=self.v["reveal"]).grid(row=2, column=1, sticky="w", pady=(4, 0))

        f = ttk.Frame(main, style="App.TFrame")
        f.grid(row=5, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.go_btn = PillButton(f, "Compress", self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        foot = ttk.Frame(main, style="App.TFrame")  # the status line, with the support link on its right
        foot.grid(row=6, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=370, style="App.TLabel")
        self.status.grid(row=0, column=0, sticky="w")
        support_link(foot).grid(row=0, column=1, sticky="ne", padx=(12, 0))

    # ---- small helpers -------------------------------------------------- #
    def _on_preset(self):
        mb = PRESETS[self.v["preset"].get()]
        if mb is not None:
            self.v["target"].set(f"{mb:g}")
        self.target_e.config(state="normal" if mb is None else "disabled")
        self._update_plan_label()

    def _clip(self):
        """(start, duration) of the part to compress; raises ValueError."""
        dur = self.info["duration"]
        start = parse_time(self.v["start"].get()) or 0.0
        end = parse_time(self.v["end"].get())
        end = dur if end is None else min(end, dur)
        if start >= dur:
            raise ValueError("Start time is past the end of the video.")
        if end <= start:
            raise ValueError("End time must be after start time.")
        return start, end - start

    def _target_mb(self):
        try:
            mb = float(self.v["target"].get())
        except ValueError:
            raise ValueError("Enter a target size in MB, e.g. 20.")
        if mb <= 0:
            raise ValueError("Target size must be more than 0 MB.")
        return mb

    def _plan(self):
        start, duration = self._clip()
        i = self.info
        return start, duration, plan_encode(
            self._target_mb(), duration, i["width"], i["height"], i["fps"], i["has_audio"],
            self.v["audio"].get(), self.v["res"].get(), self.v["fps"].get(), i["bitrate"])

    def _update_plan_label(self):
        """Show what the encode will be, live, so the user sees the trade-off."""
        if not self.info or self.info["error"]:
            self.plan_lbl.config(text="Pick a video to see the output size and quality.",
                                 foreground=MUTED)
            return
        try:
            _, duration, p = self._plan()
        except ValueError as e:
            self.plan_lbl.config(text=str(e).replace("\n\n", " "), foreground=ERROR)
            return
        text = (f"Output: {p['width']}×{p['height']} at {p['fps']:g} fps, "
                f"{p['video_bps'] // 1000:,} kbps video"
                + (f" + {p['audio_bps'] // 1000} kbps sound" if p["audio_bps"] else ", no sound")
                + f" for {fmt_time(duration)}.")
        if p["capped"]:
            est = (p["video_bps"] + p["audio_bps"]) * duration / 8
            text += (f"\nThis fits easily, so it stays close to the video's own quality: about "
                     f"{fmt_size(est)}. Using more of the target wouldn't look any better.")
        elif p["bpp"] < POOR_BPP:  # (when capped, quality is the source's own, not ours to warn about)
            text += ("\nQuality will be low at this size. A lower resolution, a shorter part, "
                     "or a bigger target will look better.")
        poor = p["bpp"] < POOR_BPP and not p["capped"]
        self.plan_lbl.config(text=text, foreground=ERROR if poor else MUTED)

    def pick_video(self):
        path = filedialog.askopenfilename(title="Choose a video", filetypes=VIDEO_TYPES)
        if path:
            self._use_video(path)

    def _use_video(self, path):
        self.v["src"].set(path)
        self.v["out_name"].set(os.path.splitext(os.path.basename(path))[0] + "-compressed.mp4")
        self._probe(path)

    def _on_drop(self, paths):
        """A video dropped on the window: use it like one picked with Browse."""
        if self.go_btn.instate(["disabled"]):
            self.status.config(text="Wait for this video to finish before choosing another.")
            return
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            self.status.config(text="Drop a video file (folders aren't supported).")
            return
        self._use_video(files[0])
        if len(paths) > 1:
            self.status.config(text=f"One video at a time: using {os.path.basename(files[0])}.")

    def _probe(self, path):
        """Read the video's length/size/fps and show it under the file box."""
        if not self.ffmpeg:
            self.info_lbl.config(text="FFmpeg isn't available yet — install it to read videos.",
                                 foreground=ERROR)
            return
        try:
            self.info = probe_video(self.ffmpeg, path)
        except OSError as e:  # FFmpeg binary couldn't be launched at all
            self.info = dict(error=f"Couldn't run FFmpeg: {e}")
        self.info_path = path
        i = self.info
        if i["error"]:
            self.info_lbl.config(text=f"Couldn't read video: {i['error']}", foreground=ERROR)
        else:
            bits = [f"Length {fmt_time(i['duration'])}", f"{i['width']}×{i['height']}"]
            if i["fps"]:
                bits.append(f"{i['fps']:g} fps")
            bits.append("has sound" if i["has_audio"] else "no sound")
            try:
                bits.append(f"file size {fmt_size(os.path.getsize(path))}")
            except OSError:
                pass
            self.info_lbl.config(text="  •  ".join(bits), foreground="black")
        self.audio_w.config(state="normal" if i.get("has_audio") else "disabled")
        self._update_plan_label()

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.v["out_dir"].get())
        if d:
            self.v["out_dir"].set(d)

    def set_status(self, text):
        """Safe to call from any thread."""
        self.ui(lambda: self.status.config(text=text))

    def ui(self, fn):
        """Run fn on the main (UI) thread. Safe to call from any thread.

        Tkinter isn't thread-safe: touching widgets, or even calling
        root.after(), from a worker thread works on some systems and silently
        does nothing on others. So workers only put callbacks in a queue, and
        the UI thread drains it every 50 ms in _pump_ui().
        """
        self.ui_queue.put(fn)

    def _pump_ui(self):
        # Reschedule FIRST: if a queued callback raises, the pump must keep
        # running, or every later message (status, progress, errors) is lost.
        self.root.after(50, self._pump_ui)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self._report_error(*sys.exc_info())

    def _dest_path(self):
        out_dir = self.v["out_dir"].get().strip()
        if not os.path.isdir(out_dir):
            raise ValueError("Output folder doesn't exist.")
        name = self.v["out_name"].get().strip() or "compressed.mp4"
        if os.path.splitext(name)[1].lower() != ".mp4":
            name = os.path.splitext(name)[0] + ".mp4"
        return os.path.join(out_dir, name)

    # ---- compression ---------------------------------------------------- #
    def start(self):
        if not self.ffmpeg:
            self.offer_install()
            return
        try:
            src = self.v["src"].get().strip()
            if not os.path.isfile(src):
                raise ValueError("Choose a video file first.")
            if os.path.normcase(src) != os.path.normcase(self.info_path or ""):
                self._probe(src)  # path was typed or pasted, not picked with Browse
            if self.info["error"]:
                raise ValueError(f"Couldn't read the video: {self.info['error']}")
            start, duration, plan = self._plan()
            dst = self._dest_path()
            if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
                raise ValueError("The output file name is the same as the video. Pick another name.")
        except ValueError as e:
            messagebox.showwarning("Check settings", str(e))
            return
        target_bytes = self._target_mb() * MB
        whole = start == 0 and abs(duration - self.info["duration"]) < 0.05
        # For a part of the video, estimate its size from the file's average bitrate.
        src_bytes = (os.path.getsize(src) if whole or not self.info["bitrate"]
                     else self.info["bitrate"] * duration / 8)
        if (whole or self.info["bitrate"]) and src_bytes <= target_bytes and not messagebox.askyesno(
                "Already small enough",
                f"{'This video is' if whole else 'This part of the video is about'} "
                f"{fmt_size(src_bytes)}, already under the {fmt_size(target_bytes)} target.\n\n"
                "Compress it anyway? It will only get a little smaller, because the app "
                "keeps close to the video's own quality when there's room."):
            return
        if os.path.exists(dst) and not messagebox.askyesno(
                "Overwrite?", f"{os.path.basename(dst)} already exists. Replace it?"):
            return
        o = dict(src=src, dst=dst, start=start, duration=duration, target_bytes=target_bytes,
                 src_w=self.info["width"], src_h=self.info["height"])
        self.cancelled = False
        self._set_busy(True)
        # Run FFmpeg off the UI thread so the window stays responsive.
        threading.Thread(target=self._worker, args=(o, plan), daemon=True).start()

    def _set_busy(self, busy):
        self.go_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.config(value=0)

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self, cmd, duration, done_frac, frac_size, label):
        """Run one FFmpeg pass, moving the progress bar across [done_frac, done_frac+frac_size].

        -progress pipe:1 makes FFmpeg print 'out_time_us=…' lines as it goes,
        which we turn into a percentage of the clip length.
        """
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, errors="replace",
                                     **_POPEN_FLAGS)
        last = 0.0
        for line in self.proc.stdout:
            m = re.match(r"out_time_us=(\d+)", line)
            if m:
                frac = min(1.0, int(m[1]) / 1e6 / duration)
                if frac - last >= 0.005:
                    last = frac
                    value = (done_frac + frac * frac_size) * 1000
                    self.ui(lambda v=value, t=f"{label} — {frac * 100:.0f}%":
                            (self.progress.config(value=v), self.status.config(text=t)))
        _, err = self.proc.communicate()
        if self.cancelled:
            raise Cancelled()
        if self.proc.returncode != 0:
            raise EncodeError("\n".join(err.strip().splitlines()[-4:]) or
                              f"FFmpeg exited with code {self.proc.returncode}.")

    def _encode(self, o, plan):
        """Two-pass encode; if the result is still over the target (rare), redo
        pass 2 at a lower bitrate. Pass 1's analysis stays valid for that.
        Runs on the worker thread. Returns (size, met_target, video_bps)."""
        logdir = tempfile.mkdtemp(prefix="vidcompress_")
        passlog = os.path.join(logdir, "x264")
        try:
            bps = plan["video_bps"]
            self._run(build_pass_command(self.ffmpeg, o, plan, 1, passlog, bps),
                      o["duration"], 0.0, 0.5, "Pass 1 of 2: analyzing")
            for attempt in range(MAX_PASS2_RETRIES + 1):
                label = "Pass 2 of 2: encoding" if attempt == 0 else f"Over the target — re-encoding ({attempt})"
                self._run(build_pass_command(self.ffmpeg, o, plan, 2, passlog, bps),
                          o["duration"], 0.5, 0.5, label)
                size = os.path.getsize(o["dst"])
                if size <= o["target_bytes"]:
                    return size, True, bps
                # Scale the video bitrate by how far over we are, with a bit extra.
                total = bps + plan["audio_bps"]
                bps = int(total * o["target_bytes"] / size * 0.97) - plan["audio_bps"]
                if bps < MIN_VIDEO_BPS:
                    break
            return size, False, bps
        finally:
            shutil.rmtree(logdir, ignore_errors=True)

    def _worker(self, o, plan):
        t0 = time.monotonic()
        try:
            size, met, _ = self._encode(o, plan)
        except Cancelled:
            self._remove(o["dst"])
            return self._finish("Canceled.")
        except EncodeError as e:
            self._remove(o["dst"])
            return self._finish("FFmpeg failed.", error=str(e))
        except Exception as e:  # never leave the UI stuck in "busy" state
            self._remove(o["dst"])
            return self._finish("Error.", error=f"{type(e).__name__}: {e}")
        took = time.monotonic() - t0
        name = os.path.basename(o["dst"])
        if met:
            msg = (f"Done! {name} — {fmt_size(size)} (target {fmt_size(o['target_bytes'])}), "
                   f"took {fmt_time(took)}.")
        else:
            msg = (f"{name} is {fmt_size(size)}, still over the {fmt_size(o['target_bytes'])} "
                   "target. Try a lower resolution or a shorter part.")
        self._finish(msg, path=o["dst"] if met else None)

    def _finish(self, msg, path=None, error=None):
        """Hand results back to the UI thread (Tk isn't thread-safe)."""
        def run():
            self._set_busy(False)
            self.progress.config(value=1000 if path else 0)
            self.status.config(text=msg)
            if error:
                log_error(f"{msg}\n{error}")
                messagebox.showerror("Error", f"{msg}\n\n{error}")
            elif path and self.v["reveal"].get():
                reveal_file(path)
        self.ui(run)

    def _report_error(self, exc, val, tb):
        import traceback
        self._set_busy(False)
        detail = "".join(traceback.format_exception(exc, val, tb))
        log_error(detail)
        self.status.config(text=f"Unexpected error: {val}")
        messagebox.showerror("Unexpected error", detail[-1500:])

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def on_close(self):
        self.cancel()
        self.root.destroy()


def enable_drag_and_drop(root, on_drop, on_enter=None, on_leave=None):
    """Accept files dragged onto the window, using tkinterdnd2 if it's installed.
    Returns False when it isn't (or its native part can't load): the Browse
    buttons still work, so drag and drop is optional.

    on_drop gets the list of dropped paths. Every widget is registered, so a
    drop works anywhere in the window.
    """
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        TkinterDnD.require(root)
    except Exception:
        return False

    def drop(event):
        # The paths come as a Tcl list: names with spaces are in {braces}.
        on_drop(list(root.tk.splitlist(event.data)))
        return event.action  # "copy": the files stay where they are

    def hook(fn):
        def handler(event):
            if fn:
                fn()
            return event.action
        return handler

    widgets, todo = [], list(root.winfo_children())
    while todo:
        w = todo.pop()
        widgets.append(w)
        todo += w.winfo_children()
    for w in widgets:
        w.drop_target_register(DND_FILES)
        w.dnd_bind("<<Drop>>", drop)
        w.dnd_bind("<<DropEnter>>", hook(on_enter))
        w.dnd_bind("<<DropLeave>>", hook(on_leave))
    return True


LOG_PATH = os.path.join(tempfile.gettempdir(), "video_compressor_errors.log")


def log_error(text):
    """Append an error to a log file. Errors can otherwise vanish when the
    script runs without a console (double-clicked / pythonw)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def reveal_file(path):
    """Open the file's folder with the file selected, ready to drag into a chat."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def self_test():
    """Used by the release build: exit code 0 if FFmpeg and drag and drop both
    work in the packaged app, so a missing piece fails the build."""
    if not find_ffmpeg()[0]:
        return 1
    try:
        root = tk.Tk()
        root.withdraw()
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        root.destroy()
    except Exception as e:
        log_error(f"Self-test failed: {type(e).__name__}: {e}")
        return 2
    return 0


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


# ---- Offline Toolkit UI: images (drawn by build_assets.py; icons: Lucide, ISC license) ---- #
UI_IMAGES = {
    "card": ((5, 2, 5, 8), "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"),
    "card_plain": ((5, 2, 5, 8), "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"),
    "field": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAYAAABe3VzdAAABqElEQVR42u3YwWoTURQG4P8/c5OokawsNaEoRazgQrSvUHDZXbfd+QLSxyi+gI/QnUsfQq0LqYoUJSSGCRSCqWbm3vO7SIoibtuZxfyru/z4D2dxDwFAkpH0L++/r1voPs+C7brHB5II4nIjgKTMwscU/ZXH+Yt7j25PLky8eHx4O316s3v95dr6jTuhA8ActMvWrYwuwA1xAeST828/5j+fPXxy67UkAwCcHOc702GSR0lSlOS6+rik6FGaDpNOjvMdAOCnN6O1cK377u79Xt8CHEKGqynuv+MGkTzCvn6ejeOv+WNLaB/0N3oDC3BViQMAAhIyC/D+Rm+Q0D6wELjX6rgAsErbX0YAYKvjCoF7odUOm1ouA1EfocmIVjtsmiDVorp/nQSE5RrXkPfHaah5GmADbIANsAE2wAZYPVA19skIUjUkSgBBWlnEU7oEwGvR5dLgdKks4qnFqKNyYVyha+KDyoUxRh1ZhuJwPJyNPMJIpEpbFMDVt3M8nI0yFIe2tT3Ii7LYP5s4lZCBSBUtjkAkJWRnE2dRFvtb24O89qcP1v149BtL8nqN2wAM2gAAAABJRU5ErkJggg=="),
    "field_focus": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAYAAABe3VzdAAABy0lEQVR42u3YvWuTURQG8Oec+w6h+SA0lSYVJJ0EuzhpoZul4mRdCv4DDgUNIh2FUHApFBFx8x8QuoijBbdC6+RSQSi0SE0iNG3IR8nw3vM4JAVxdHhzh/eBA3e4w49zucM5AgD1OnVzU6z2uDXLbvZFNuce5orxTQgFSYTCfif6Mej7T1IYvH77ofz7yiRXh+ePzu7nc9n3d+9lbty6A8xcN6hLxmeeOPul+P4VOPgy/NnrD568+TjzuV6nAgBqqxfLW+uep0ckyZikMfkYyfj0iNxa96ytXiwDgDx70LiWL2W/PX1VqFSqMO/hVAAk07y/nhkwAs7BN0+g7152m7324LZ6y2wsrRTmKlWYj+GcA0QBkYRLAecAH8NVqrCllcKct8yGTuWxtrBoBCCimHjGBllYNE7lsRYVS9H8dFkAQEQCAI4MOl0WFEvRvEJIDaBz/0YVgJA6ge/wHy8ecFJgCkyBKTAFpsAUOHkgA/ZRQRGz8GRmACiinXZ8fN4iARgD6OXYYOctstOOj/Wyh53DfRUAZACdHBt4uK9y2cOOOh1u7+12G80TqIvgvR9dIhMuA7wHXDQaO/d2uw2nw+3wB/fQVx8S+vLoD40rMFuaLzX1AAAAAElFTkSuQmCC"),
    "field_disabled": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAYAAABe3VzdAAABg0lEQVR42u3YT2qUQRAF8Peqvjhm8mcmE8QYRQmBcesdFJfuss3OC0jO4QU8QnYuRe/g1kAIimIkoPMZYtR01XOhA9nLOC18b9XLH0XT9CsCgCQjmYeHx9cHS4MnRntU4uddSQSI2UYgqcavvEnl8/asfbq9vfFpauL08OHt5OHi4tVnKyu925FCiQLO2jYlCmi8gRtxevrj3fn598c37wxfSDICwPH7L/cHg+FLa4CIiwBg+Ge8y0yk+4JnAdp28mDj1torHhx8vTZY4uvhqH8jIhKgY65RuLtNPn/72J7pnvV7uTdaX94sVeAAgF4icrS+vNnv5Z6ZcSciRRpRSUhjRMqMO03jvlUiYMZqgJCsZKJx3zJJqoh2aYqAJBlm/9D9ldNQeTpgB+yAHbADdsAOOH+gKvbJSFIVEiWAJK1EHDXuApkV/VazcVeJOLJM7bsbpVQ900u5GzO1X3/tHI9XT6Sym8XovuCA4k+JnsOtU/wu7kap7I7Hqyf/x+qj5uXRL8vp/YKQdcvfAAAAAElFTkSuQmCC"),
    "field_hover": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAYAAABe3VzdAAABzklEQVR42u3YsWtTURQG8O+c27yXZyMGQqtGKBQCdnR0tqLQwa0FHbr5D0h3cS/+A+4KZnMQDJJZKBS3VggUCsbYEIhNm9d3X979HF6DQvfXO7xvuPOPczgXzhEA4GuqvBH39f3gNivBqyDQZ6Yyu09SUEBEhFm68MNa90lS+/bxizu/5yYhqSLivnz4+WTx5uK7ldatlfoSEN5wEEUhoQOSqWI8BI57f47PJ+cvnz6/1yGZCzrtwfp+N+H0lCQ5I+lYfBzJ2fSU3O8m7LQH6wAgnz/2l6Iw+P7wUeNutQZHwuRlR6Eh5+1GdnEG/dYd/YoT+0CRYqe11mhWa3B0MCLF4+YFEQHoYKo1uNZao4kUOxqEZrO+7AhAcA2wq9L8rS87BqHZXAgjsxpEvBwmD3y5QYOICCOzqgDphezq1wMgH2P/dP85FZ6nBJbAElgCS2AJLIHXD6THPiogQvpnzE0imsTZkY2FAJwPzkuDs7EwibMjtUnWHp+oAKAXzc4NHJ+o2CRrKyrY7R2O+hdnUFFk5L8VsOjKkYBovnb2Dkd9VLCrG1vNYWpn2wd7VuIJjAgykeJrKQKKIIsnMAd7VlI7297Yag69P32I78ejvxHgfwWAaXLzAAAAAElFTkSuQmCC"),
    "button": (5, "iVBORw0KGgoAAAANSUhEUgAAADoAAAAsCAYAAAA0NKuuAAAD/ElEQVR42u3aT2gcVRwH8O/vzcxmF6am3W7zD7pmaY0SYw49NJeeemjUQ04hh4gI9mKDBEUPRSEHocGLGFDaQGkOSnOQlELBf7G0iHhIoVDaGEvddKWhWXZdG0Oy2c3u7O/nIbtxnc5uYi3ZWTIDjwlkd3kffu/95s17PzoWPo3dcCnskkv/D58llxrkaUGpApRcBpP/AyVbqwbfaWj5XbbCVoNSz4lWY2Y6zgDo04nTr5qm73VlcBdp6GBmItpZq4hAKSVSwD3Oq9nV1dyX77758TcApOdEq5qZjucrYalC1qW+waP6lckbeO+j19o6Xoic27Ov4ZVQsw7DD0CT2oazQMhngVTCwsrS+rf37sZOfTJycbFv8CiuTN6wnLBOUDreH9avTT3AmfFTPW1tB6banws0Kz+LUkpsw7iWCUiYmTir6PffMonFxT/6P3zr3Mzx/jCuTT14DKuFG4899shJZ1a0offfaHm2ve16R3egSTXAIiJNBEQAuYBJApBSRMqAFTxgPJNd8fU9/1Lk4g9f/5RO/yVihyqn5JOMMcLtTecPdQZC0GBBNuYykUseMlTsywZahwbrUGcgFG5vOp+MMZxGnXLIsGp0fLg31GL26gEwAN21T9B/cr+uB8ChFrN3dHy4t+j6F1Y5rJSUafpPhpqNUqJz/VXso4SaDZim/2TJ4RTR8mel8vm0bsMvAKDI/c5S2JThF/h8WrctolRprUs+vxYp/odQP1KCAnx+LeKUSZzmKNXFeK2yqHBYze2etxcP6kE9qAf1oB7Ug3pQD+pBPagHrWPoTm9MP9V38Ap9d4TmMoUYuLjHVg/v4MU9I3Cx71tAS3uhsp6z7uSzBABcP05wPktYz1l3yi3VIirLS+mJVCJfN6O42EdKJfJYXkpPwOFIQtmjCYBHhs9eTcZXpq0MFADL1cN3o2+WlYFKxlemR4bPXgXA9qjaI8oA2AwSbt+MDs3PraVQgA6Ctbl/Ku7Abe7fESwUoM/PraVu34wOmUHadGw1R7n14F6+MHY5fn9+YSA6m0laa9CZWYjAIBdQCUIEZmax1qBHZzPJ+/MLAxfGLsdbD+5lW0QBOB8y0aNEVrqOhNWlL358uG//nkv+hmBHLqsd9umKiEAMgTBq0pgBzhOtp0GJhQI9jK19//P1WwOfnfnq164jYbl7K5G3IyseGxabFulsNGJzywCgjX7+zsvB/eag3iAvKp0O1/IgmC2JWuv0y6M/Vyc/eHvsOwCFSGcjYnPLeQCF7UJLWOXQqOxe6xTEZXd7k+0e7UvZZC79rarUM9SifkFsYKmE3KqGofTFEkzg7mINedJiDfuPubX8ZsuKlO3WGT1RXY/bCqr+BtmH3tFeF95oAAAAAElFTkSuQmCC"),
    "button_hover": (5, "iVBORw0KGgoAAAANSUhEUgAAADoAAAAsCAYAAAA0NKuuAAAEJklEQVR42u2aX2hbdRTHv+d3b26TNCa1Zold1tC6uUEte9CxvgyEPazqQ0EofdgQwT2t9smKCD77IioMJhsbFERWUCqDDdSVsWF9kHbSB6Otc1vbdf2zhlqTpkmT3Ht/x4c0I97etHWKSej9weEXSHL5fTi/8/ude86XjkXfx24YArtkqP/gt1SlDPxfgVIZUKoyMP43oGSxrcD/b9DSmbeD3QqUOk40uUaHFyUAeueDd1+r99S9odRxu+rig8xcEVAiYkOn380c/ZJez33x6YcffwOAO040idHhRb0cLJU5danr0FH16p0xnHzr1N7n9kfPN4bcr+5t0VDvBxRNVtShZl4gvQoszOSxEs9+O3V/9szgwOWFrkNHcfXOmGEHawdKx7uj6s2hWfS9d6YjHH5mqO3F+rA/JFlxCWYJYq7swUQEJgE2dUmrcUET4+mlpaU/us99dH70eHcUN4dmN8Eq0cCxTVdOej2lvH7qzWeb94VvvfSyL+QPwZAmKdIEsdyA5MqZlCBpgoiI6p+GEQxr/uSy0rXv+f2Xfxz5IZ1OMFtBhd3hE5+WiOwJXmo/4gt6G2Do2UIsExWs0qN0HXoWqrcBRvsRXzCyJ3gpPi1hOTw3gRa/FH39vZ3hiK8zEII08lCpitMKEoCRhxoIQYYjvs6+/t7ODa6/wQqbTEl4fZ7TkZY6KC4wM6p+MAOKCxxpqYPX5zld5LDzaOldKTS3OOz1M6RZOymiNCG8fobmFoctHqVyuS65PWqrogHMIKLqhyQqrFXRALdHbbW7++xilGpiv5bdxgybbG73vL04oA6oA+qAOqAOqAPqgDqgDqgD6oDWMGhNvG2XL3DvHDS7bkyb+UL9tFZqRkRgM19Y+3agjyunuawZy6wShAJZM1tTgcysEnJZM2apBJf1KKcSawPzMzmYeu3UjEwdND+TQyqxNgCbloSwehOAvHD24o1Hc6nhZBxC1WBwFfuVJaBqMJJxiEdzqeELZy/eACCtXrV6VAKQvkbC5N2p3tjt1HImAdXlhlGMhWqI2dJ1uNwwMgmosdup5cm7U72+RnrMsV2MyqbmBnlt8MriwwcLPeMj6fjKHFRAsqpBKiqYRKFCXilTVLCqQQKSV+agjo+k4w8fLPRcG7yy2NTcIC0eBWDfZKKVpSy3h6Li+q3v570+/9esBw6m/lQOCFKIAGKWMA1UyBj5DFEiDrof02lqYu36T2M/93z5+VeT7aEo/za1pFshy7YNN0xpbQu4pieSAKC83d/3SkPgqZOqGy+4NByoZCNYz+OekcWviWRq8LNPzn0HwGxtC2B6IqkDMHcKWoQVNkYlc6UFGrJkthrvtLXPJcFc/Cy20DNUQr/AFmAuB7mdhqH4xyIYo7rFGvykYg3rw6pVfrOtImWnOqMn0vVUm6DqL1GBG3K3Sd30AAAAAElFTkSuQmCC"),
    "button_pressed": (5, "iVBORw0KGgoAAAANSUhEUgAAADoAAAAsCAYAAAA0NKuuAAAEOUlEQVR42u2az08cZRjHv887M+zsUlgW2MWF7UaCsQdt0xPUpKemhrYHD0pqtDWojVZ76Nm/oxeNNKgRtQf0VlQk9UhBkx7QQ42wdvmxZYHCQpdhmZn38QDbLMMsYG1gNsybPHknmcm87yff93l/PQ+dTn6Cw1AEDklR/8O35FEGflagVAaUPAbG/weUHLYT+H6Dlta8G+xOoNSRiGsjUxkJgN7/6PqFgBZ4R9PpZS3AL0opifYZlRkQQrBZoL/MNf6jYBa+7v3sxgAA7kjExchUxiwHS2VmXWo51q5O3x/F62+90RyLPv9pNB48n2yrRk0E0HQLzLRX93iGhUDEMNdUrCwC6fE85jLGj9m5fz7+4bvvZ1qOtWP6/qjl1jE3UDrTlVTv9KfR/cG7HQ0N8f7j7bVNDS02q1XE0gYxgw4Gc6NNIrBQwNY608K0QmOjy7MLC5mur3q+HDnTlcSd/vQ2WCUZPr1tyckbK8q5828+F2tq/vWVs3WxhoS0pE2KbW5AFocR9tmKbUoJkhaIiCgcZSvapNfOzSivJZJHv/n9t+F8fonZCSrcJp9sSiJSH+05eSrSWBuV1rqx4cskAPLAIkO00RcAWDeg1kaldfJUpDFSH+3JpiQck+c20OJLcan7amc8Ee5sTNjSLEAlD28rSABmAWpjwpbxRLjzUvfVzk2uLbDCZack9FDgSrKtGmqVwszwfGEG1CqFk23V0EOBK0UOt+WldK0UeohO1EQkbIuFdzdEW4tt2aImQtBDdGITUpYoym6DkvSg1qoGJJiJqAI4iQBmIjUgoQe1VrfNjJuPUkWM17LDmOGymzs8pxcf1Af1QX1QH9QH9UF9UB/UB/VBfdAKBq2I03bZQzjtHXTNMFNWQYCIK+bOiIjZKgisGWZqN9AnN6hGXo6tLAooqiIrRUlFVeTKooCRl2OOG+GyivLqY6M3PZ6HtW5XzJ2RtW5TejyP1cdGL1yCCMKpJgB5q+/mUGZyaXB+ShFaABZ7WFeWgBaANT+liMzk0uCtvptDmzeAW1R1KioByCP1hPSDiWv3hh/NL88JtSoIq/hTL/gs80ZfAKAqCGt5Tqj3hh/Npx9MXDtST084dvNRGT9aJwdvD2RmM5MX7w7lsg8nhAowazqkooJJAEJshij20YptKipY0yEB5ocTQr07lMvOZiYvDt4eyMSP1kmHomWjaQKACMeSWi6bprMXXk20tR2/EWsOdXoxbJidWf15fHzs+tDAL1PhWJJz2bTppqhr2HDTlNZIWEst5gBAufzeh+eqQzVvB4LiJU3HCwcaCF7D3wVD/plfXfm274vPfwJgt0bCSC3mTAC2U80dA8El8YtSo5L6oBM0ZEntNN5raJ9LpC8+ix3yGQ4if4EdwFwOcrccBi4J1JQ6pFeTNfhpkzWcP/Nq+s2uGSl7zTN6qrweryVU/QsnQPUIPMuWTgAAAABJRU5ErkJggg=="),
    "button_disabled": (5, "iVBORw0KGgoAAAANSUhEUgAAADoAAAAsCAYAAAA0NKuuAAACmElEQVR42u3aPUgcURAH8JnZt/eBt8odnJomFhYpDoJIOBBiGsFCDlJoGQhaWFqEFIoWFkoaSZHSQgkEKy0CYiGIkBSCBJHAFSksUkU5UPBO9Ny3808VCEFlbeId9wa22mZ+7OzMe/uWnz+eplYIoRYJB3XQJg1z182ppX7vw9uDiIjo87f5EWF5TbBPFfREVZmI/3O6IBGBMP0gNt8V+vHls/mtf3O9Kfi2rvv15zsZ7JnRT1/ePArSwXIy6ZfaMh4piKJIH/TpeJ6QMNFFLaJ6PdysXlYnX714/+tPzrFLd2qp3xvsmdG13eliNug4yHWmSok0waqqQsHC9JCXQmFVNZEm5DpTpWzQcbC2O10c7JnRqaV+Lza0XC5jfX+2KxMkN9tzie4wVMssTMRCxPzwbxwzEQuzcBiqbc8lujNBcnN9f7arXC4jNnRnta6wWMnmU/kwVMvEplGbDBObMFSbzafysFjZWa3HL92NvbnhIJMesaFqIyP/xtpQNcikRzb25oZjQwFMtmUMEQuaZn6woC1jCMBk/DnK3KcAQbVp5ixURQEi5r7YUONJbxSBmBuh8cRtT8xRBDKe9N6ndJt2BXRb7m6t66AO6qAO6qAO6qAO6qAO6qAO2vjQJtpvx879RqiN9MjzmNBEO3AA8DwmG+lR/NIFDoWZWESb59uYqDATAYf3Kd3li5olgjZPDUP5omaJmZdjQ0cHFrartcst44uAYBveSLDGF6nWLrdGBxa2Y0OHxpPChifOKlcV3xfTyFgQrO+LOatcVdjwxNB4UmJDC4UCjxUXT2rVeun89PrY98UACiIoUSM0KIAICih8X8z56fVxrVovjRUXTwqFArf0sSHf9VdKSxwEuyWggzqog/7P+A3RBVITIp8RJgAAAABJRU5ErkJggg=="),
    "primary": (13, "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"),
    "primary_hover": (13, "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"),
    "primary_pressed": (13, "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"),
    "primary_disabled": (13, "iVBORw0KGgoAAAANSUhEUgAAAJIAAABECAYAAABqHGy8AAAEQ0lEQVR42u2cT2gcVRzHf7/3ZmZnJglemm0qIsiCh0YRI1hRA/VvvAi9BIrV4966odTamyRhQaQtpWygF70qpV4CnhpIjDQt1MNCLkXUIEVaJCfT7M5sZva9n4dtsFWr62ydJMv3AwvLMu/tzrzP/t5v5+378Xuv/kQA9IrCJQAQCUAkAJEAgEgAIgGIBCASABAJQCQAkQBEAiATTq8dlKsl57OP19rvT/5QeGr8xXcTkUkWecWKfdwYQ8zMD7bgv/QhD+uc//m9pZsPyA8+kQznKJyhzZ8/AP/Xdt1fD+niYBERrR1SWt0hpmta66+u/rzy9Y1Pnt7aHsNePOBeVv+/uFriY+Nr8tG54G3PLZxRWj9HzJQkCRmbPuTMIdJOiLR9pHZc8goFsiJkjFndSlqnL1Y2F7bHMqsL+tknpzI1PDJV16Ojo1J6JpkJ/OBzpfVIHEc2TVOx1v5NJOrSjoyHditSb3300PiRnkvWg5msNZKmiaRpIoWCf8B1vQ9eeEvzEyPht3ZoUX9/44DkliOVq78487UxU79+Z3r/8Mh01IpMFDUsM6t7j0dx2cD/AHdQzEpFUcO2oqYZ3r9veqW+Pj1fGzPlasnJdWo7fW7gdT8IFltxnFqxzrY88q/fHkxtOze1/fGy3Jc7KaXavh+6W63ojdrxjaXcItLJC0EoQnNERFaMRgTa2xHKWqM7YylzJy8EYW4iaUMTQRgcjKKmZVa4hbDnZVIqijatH4QHE60nchOJhY8ysWScKcDu1EmYWYT5aG4iWZLDSdJi3NDsK1SStJgtH85valO6mJo2ITfqr1zJpG1SWhdzE8kYI0xwqA9tImPa+d1HQiTq78iERVuA1X8AkQCASAAiAYgEIBIAEAlAJACRAEQCACIBiAQgEoBIAEAkAJEARAIAIoGdFUlEsJ+tT8k6ttm2I2nNgr2R/WgRae3k9+d/Y826qx1Epj6LRNp1yBqznptIinjZ83whIosh6Bus5/kiSpbzy5FYLkmn2Av2t/VPTGIRYRa5lN/UpulKHMU3w3BAiVhEpT0/rVkbhkOqFUc3PWOu5CbS+RNxxEwVIiLF2iBX2tu5kVLadMaSK+dPxFGupf/OnGouNRvN2WJxxCUmi8i0NyMRE9nh4j630WzM1o5vLGUt/ZepGGn9m8fskam6fvOdl5bXfvyVQj94zfUKnKaJlXs1k1CMdDcWIyUSEun80BcJw0HluJ66+9vd2ZefH561Q4v6y099k+ud7fnamCEiOvthcyZuRROm3V4NglCF4YByHJdRg2tXxiByHJfDgUEVhIOqbcxqFDcn5iobM/ePae5LJMfG16RcLTlnT8ULt1a+O0TWTCbt9DIz3VZKC3Kn3ZYLOcLMt9M0uUxiJ6/funboYmVzoVwtOb3U2O65YDsAWLQFEAlAJACRAIBIACIBiAQgEgAQCUAkAJEARAKgN34HXpm654dpCW4AAAAASUVORK5CYII="),
    "trough": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAMCAYAAAAQw/9NAAABU0lEQVR42s2VsU7CUBSGz7mXUlqXklA2F1cd2UyccHd24x1MNDyBg5MvwObs7mbCxgYOhMQEjInBmjYhtrnAOb+DDkyOpd8TfPn+5BwGwMyM0WjkHR2eXBk2l6I4FhFiZqYSAABrLVnDLwp9eH2b3HU6nQ2AX4EsQ5M368fwoH6W50rOOSrJbVeSfN+nMDSUf6+f4dUvoohTXiwWQVBrP7Va/ulymW+Z2XLZdjslAUi7HdaSxA2L7fKcP9/z66gZ3KZpsWFmjyoAgE2zGXhZWtwYMPWKXJWILFUHW+SqYOrx14eDiNJ+Rv2vIpG1hgxVHCMq04ZfVwBanXrQhl9XUZkaBg2C0BgikgqFkyA0hkEDU0hyn2VuGMeBp6pbANhjOajqNo4DL8vcsJDkvvKH2gDgKOJ0Nh93VyvXh8rEWosyS/69OkBlslq5/mw+7kYRpwD4B//G3RS8f+vTAAAAAElFTkSuQmCC"),
    "bar": (0, "iVBORw0KGgoAAAANSUhEUgAABkAAAAAMCAYAAAA6RnGKAAADrElEQVR42u3dsW8URxTH8fdm9+bO3osQAduCFHEd5R+IozQopS2Zwl3+AYrUSDQWTST+AWhTpIoVpTiXUaQIiS4tTUQFwpGCbAw+393e7jwKKBIpwJ19u+O9+35qa9+buZmbkX9ane6KubuiYeeLn/3qla/viJU3Eue/GYdBUFEnH6MiJlXRqf6kuj6ma9Gi1df6+5jgI7LI9d/fh9Zav5L5OMcQbBbFZjCFFmn8M+lDZ9+cRRr/mfrQ6hezRRr/RH3UWP9/+9AIDfy7j3jl/zP/FmvDaMSz/wPDsboOGI149k/xcVpt9bXePjTCmTvD7WTnfYBGPPsr+DqxaQtqxLO/hu9/m0l9ra4PjXj2V1ZfJ+8j8vlvF+L+oXH+99DY+4fWtlTn4/6hM5vKxbh/aH19zM39Q8/Wx8LeP7TW86/59w+tpo9JHmsWXKvlQhkeinO/Hz3764fHe1/msmtORUR2dixZPXj667K/ujUsXkoRRqKTNkwAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCABIjAHkbgohL25J2Uhn3B71rB0s39/a01NvfHl56NTj+KfMrm/3xP7mIpBO9+UEAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgBCAEIAQgDC/SN2APLuTRARKVpZ5vP+6f5oefm79PXg+Fa3fW3zZHSQq6oXAAAAAAAAAACAJlF1IuLzfj9vd7NNe92/5URsa1gcBVFNmSEAAAAAAAAAANBUKpIWwzyos600STobZRhO/psfAAAAAAAAAAAAcjHfBAnlWJJkacMxGwAAAAAAAAAAYN64shw+SlwnmEhgOgAAAAAAAAAAQGOZBZe0QlkOHjkR7XXSy07MCmYGAAAAAAAAAAA0lYkUacc7C9pznyxdenAyOtjP/Io3sdzEeBMEAAAAAAAAAABIk978ELPcZ5kfnZzu51n2wN377dPjF9fXtwfjw17Xr/nUtZ2JMVkAAAAAAAAAAEAaEH6IS9vOdzNfnA56n/29vP3nPT1OdsXc/cca1rtf/eLbK8GsTFLX+by0cVBR/eiDtcquVeLWl4bV1wUff4TmF2r9KftvqvrK/qv6YQu//pT9F7PhuV9/yv6bqr6y/+p8wMKtP2X/TV1f2X9VPZT1F7eJRs6/sv9mVYT9F7epuZl/Zf+dpTD7L26jOuFvfrRaGkL5sCzDj0fPn3z/x95aIbvm3gCyyk1KAPcTHQAAAABJRU5ErkJggg=="),
    "header_deco": (0, "iVBORw0KGgoAAAANSUhEUgAAAvgAAABUCAYAAAD3Y0NfAABAyUlEQVR42u2dyXIbV7a1184G2aBjJ9GSZYWqXBqUHdcRDk8d4cl9Bc/uu9xnueN6hH/kiJpqaHtgu6yQpZJESSTRZp/nHyj38cFhAgRIgGpqrwgFQQIE0YlcufDttQkikUgkEolEog9RZHxUAJzmcz7NHwkA/va3v1FRFJTnuXPr1i06Pj7GaDRyq6qiXq9Ht2/fdsIwJKWUGwQB9ft9x/d9qqrK3d/fd8IwdAaDgTMYDNwgCNyDgwMvjmPv4ODA6Xa7bqfTcaMoIs/zyPd92tWdLopi4XPf95X1NVWWJZVlCaWU8jyvLsvS/F4FQOV5rsqyRJ7n/P0qy7JaKaWyLFN5nqvJZKKIqBqNRsp13booirooivrk5AREVKdpWqdpWp+fn1e+76s0TaunT5/WSqmqqqrqyZMn9Xg8rgaDQRVFkXr06JEyn6PvvvtO/fDDD/jiiy8c8w7keU6ffvopZVlGRVHQnTt3EEWR0+/3Kcsy6na7tL+/j/F47AyHQwBAEAQEAIPBACT/N0QikUgkEok+GDNvGnrT4NvnExv6qqro+PiYiqKgsiyp1+s5pqkvioJ833cGg4ETBAF1u13H930iIqff75Prum6n03HiOKYwDN1+v+9GUeQMh0OXzf5wOHR833d839+qwTeMu7LOUgBQliUB0Abf932VJAk8z9OmvSgKsNkHoJRSaj6fw/O8ej6fwzT0SimVJEmdpqlyXbdO01QVRVEnSaKKoqizLKurqlLT6VSlaVorperRaKSUUvV4PK7jOK7Pzs7qPM/rV69eVefn5yqKono0GlUAMBwOawBIkoTYyDcHX/j555/pzp071NxvOjg4oOPjY2RZRqaxj6KIoiiixtS7QRCoTqez8JiLwReJRCKRSCR6v828fR7ZCT6beQC4c+cOFUVBRVFQlmXO4eEh4jh2iqKgIAicTqfjRFFEQRCQ67pOr9dzOp0OxXHseJ5Hnue53W6XwjAEEbmDwQBBELiO45Dv+26/36coitx+v+84juMOh0OK45jNvQOAfN+/Tiqv2k4nSXLBxJupvJHOoygKVRQF8jxXnPDXdV3PZjN4nlfneY66ruvG2KOqqrquazUajVRd14qT+jRNked5DaCuqkpNJhOllKqb61d5niu+nrIs6zRNVZ7n9Ww2U3Ec10mS1C9fvkQcxzUAPH/+XAHAgwcPYKbzWZbR3t4eZVlGAHB0dATb0AdBQEEQkGnm9/f3CQCa502/Vjz5PyQSiUQikUj0XmA2lxl9AkBffPEF8jxnc6jNfFmWtLe3R4PBgJIkcXq9Hvm+70RRRGEYUlEUFMex6/s+saEPw9AB4BweHpLrulTXtRPHMQ2HQ6rr2gnDkFzXddhcOo7j9Ho9CoLAISInjmMyk/vGgK+N1xhJvDbsZVmiKAqN3pjpOwDM53MAULPZDEVRKADI81yn80opNZvN9Cd5nivXdes8z5Gmqaqqqs7zHEmSqLquleM49Xw+V3VdK07oy7JU0+lUAdBmng2953nq9PRU+b6v/zmOo9I0he/7Ko5jzGYz5HlOe3t7lOc5ALidTkf97W9/AwCMx2OHTXxj0J29vT0AAJt54C12o5Ryer0efN/XBwGDwYAAII5juK5LAOA4jiT4IpFIJBKJRO+YmW8z8BdMv5nOHx0dOQCQJInTmD2n1+tRm5l3XdfZ39+nxog7nudRGIZOEASO67rU6XScvb09cl2XPM9zXNd1wjCkTqcD13UdIqJOp6NTYyIi3/cpjmN9mogcRnI8z+OEnWwzb3Lyy8x8G1rDH4uiUM15qigKMGbDxr2u67ooCmRZpuq6VozY1HWt8jxHURR1Y+BRVZWaz+eKiDRuA6Auy1L5vq9Go5HiA4e6rmt+F2A6nYIN/Xg8RqfTUePxGL7vq+FwiNFoBN/39TsOQRCotheBxc47g8EAnU6HD46IzTwfMAVB4MRxjCzLqNfraWMPAN1uV6f+pskXgy8SiUQikUj0nmA2zF+byTwA9Ho9pyxLqqqKPvnkE6dJ48n3fYcTdtvMs6HvdDqO67r68p7nOVEUUb/fp7quHTbwQRAQEZHjOE5jIMn3ffi+T2VZku/7jN4QEVEURfq+sLm37pMyE3qLp9fDpkVRwDT3DQKDoih0As+pvOu6dZZlajabIQzDOssylaapquta1XWtqqpSdV0rTuTTNAUb+TRNkaZp7XmeKstSFUWhDb3neSrPc9W8E4Asy5Tv+2oymcDzPNXpdBQA8Edbw+FQp/FBEKgmZYdh9vVrgBGbXq+Huq6dbrcLz/OIk3hO5dmsx3EMx3HIcRx+zMFPwjovPpFIJBKJRCLRe8DMl2VJg8GA0jR14jimOI4piiKNy/T7facoCup2u04URc4yM+84Dg2HQ+J0nogoDEOHP/q+j+agAEREnuehqirH8zw0Jl6VZekAQBRFaAZyF9594KS5LZlvGm2glFJ8PqfwnMq3Dbx6nlfPZjOdyDMjn6apYrSGk/iqqlRVVYqI6iaNB5pEPkkSKKXqsiyJfzZz88DbdxayLDM5/5VmnpP6PM+1gW9mFIjNe5O0w3w3o9fr6cdpMBgQD84Cb1N3TuijKNIpfBRFyLKMgiBQbOZNU99gS2LwRSKRSCQSiXZk5pcZ+42Z+bIsF4Yq2czb3PxwOETDwrs8iMnMvOM41O/3qaoqx0znOT02cBsAoOZnojHCNkdP6wzJGhiOOfCq5vP5AmJjJvKc0GdZppqDgJpNPRv5oihqE6uZzWaKcRoeeuUhXNd1K+Atp8+pPOM1bOhNI5/nOXU6HdVm5vm8MAx1/WRjuLWZN4287/vU7XYBAFVVOXEcg407p/DAYiLPRn5ZGh9FEdI0pTAMFQCcn58Toz9pmgJ4+44BDyAnSYIgCJQYfJFIJBKJRKLNh2BbB2CB7TDzvu9zwqvNvFLK4brKpm9+YQg2DEOnqcE0mXk018fIjYnXLLzDsGbzjWrum+Juec/zOCnXaX2D19Rs5k0Tn2WZYhPP7TNs6OfzuXIcp66qSrGRZ6xmMpnogwTGahil4RQ+TVPFt8lO4ZfhNcCfyTybeQC4desWJ+3Isow++eQT5uB1Gs9oDRt513UpiiKYhn7dRH5VKp+mqTbySZLA9/2aD2TY1FdVpSTBF4lEIpFIJLrmEOwqM39VZp4HYA0sR5v5qqqcfr+/wMt3u11OjNsMvf7IKf06HrDh1GGaeTbXnBQzM8+sPPPzjMAwYlNVle6VN2somZefzWYarzFTecZr5vM5wjCs+SCB2fgkSRQn8uZwq3m6KAriRVCmkW+Mt9PpdNStW7doPB6DZxHMVH6ZmWdGPs9zOjg44JpLpYdcr5jI82lO5Hm2YD6fo65rBYCHhJFlWQ0A3CbEz810OhWDLxKJRCKRSFCbq3Dz8/ncAS4OwDbm0LkqM8+pflVVThiGtGoI1uhE1y0sbC6XMfMtC7MWtry2MfPNAii0MfM8/MoGnDGb2WxW24hNnud1WZaqLEsFoJ5MJorxHU7loyjSBwfmoGuapjwUi+FwqI38+fk5uGKStbe3h/Pzc8RxTABw+/ZtbeJtxIY5eV4qZQ68uq57IZXnJJ4/hmHYOvgahqFGZ7BhIl9VlQqCQM3nc3BtZ3NftZGfzWZ1c5/0YDDjR2LwRSKRSCQSiZlfcwiWuXlzcdSyAdjrMvNNGk+XDMHqZL4x81h3qdSSnvm1mPmqquqiKOA4jmbmR6ORKsuyZiNv8vJpmtZ8nQDq8/NzbUqzLFNxHNd2Kj+fz9VwOMRsNtO4TRzHNJ/PFRt308wDQLNtV3fIMzff7/c1J78slQeAsiwd08ybhr7NyIdhqM7PzykMwwuG/jqJ/Hw+r43nS+V5rtjMZ1mmkiRRSZKos7Mz9Pt9fdnJZKIE0RGJRCKRSCRDsNdcHMUG3Dbz12XmzVrKFUOwZpsNyrKktk2wbOSZ8zZT+mYfVG2a+VXMvOM4NbfZVFWlE/qyLFWWZfV0OlVlWSrP86qiKNTr16/XQmxms5na29vDfD7n5VV0fHyszs7OeGnUQjJvojd2Mm/3yTdbYbEOYmObeeDtuyFs0hmzMXn55nGiIAjU6ekp6rrWCfxVEnkzjQ/DUJ2dnWE2myk+YPjtt99Up9NRx8fH+Pnnn/XjyOeLwReJRCKRSCRDsFtYHNXv951tM/NNv/wqzAZLEnpt3htjqtiQboOZb3rla2bmeaiWU3qlVH16esqVmTXwdgCWjbx5e802GzuhN7e7AtAtNmzmOZnnTnlO5T3P00ae0/h1EZswDBUbeJ5ZaEvmkyTRiTzwFq8x6ju3lsjzux1BEKjXr1/r01EUqV9//RVtQ8Ri8EUikUgkEskQ7A0tjromM4+WZN4eggUA1VRG1sbm160x883RQj2fzzEYDKqTkxPdJ88tNnEc17zl1URu2MDznTBRGz7NS6MYr+G++cPDQ0rTVKfzWZbRcDikLMvo6OgIyxCbtirKdRAbPioy8RpO400j3xx41MBuEvnHjx/j73//u/r1119bm4CiKNJf6/V6kuCLRCKRSCSSIdgbXhy1MTPP1ZS+72uzaTDz4IpKNPy853k1b2XlpVFNyr4VZt5cEsV98mzm0dJkYyfyNmIzHA6RpukFxMbul2de3kzlgbeDsFEUgZdEtSE2RKSRmnUQmyAIFlh5NvMmXpMkSc39+mzmAXAn/9YS+SiKVJIkZJt5U71eT02nUxKDLxKJRCKRSIZgb35x1MbMvOd5dVNHqZdImUOwnMwrpVSapozcqMlkwia1dl23vi4zbzbZtBl5tLTYtCE2vPnVHH5lXp5TebPBxkZs0HTLM2IzHA5VlmV0E4iNidewmVdK1WdnZzDN/LYS+cvExt8+EBCDLxKJRCKRSIZg393iqKXMPCM3Dduu2Xmz1cZ13ZqHYcfjsXJdt55MJijLsp5Op7XjOPW2mXmTjbe/FkURsdneFLFpWxTFRt6uoLQRm729PWUb+W0jNqPRCIPBoDbxmtevXyNJktoY9lWvX79W20rkbTPP3/vll1/ixx9/xP3798k8/8GDB8Lgi0QikUgk+s8egm0wGs3Nv4PFUZcy84zZ8BBsXdeKh2C5zaaua5XneT2fz1XDuNdEVCdJwteptsXMm+Y+z3Pirn67kvJjQGz4gOf169doDlrqly9fXsBrAODZs2dqF4n8l19+iclkQmzwHz58iBcvXjj37t3D06dPsb+/z+9k4fnz52LwRSKRSCQS3Qhm814PwTbpvObmd7U46irMPA/ANumzsodgTW7e9311cnKiwjCsAdTT6bSezWaI47ieTqdbY+YbOYPBAP1+H3VdOyZi43merqT8UBEbk5U/PT3VBz3Pnz9XV8FrrpLIHx8f03w+15/v7++TaeI9z3PMn3n79m2HD0TE4ItEIpFIJNqFmQcAx2i7uZEhWJubZ9TGHoLlJVJm1/yuF0eZpn5dZn4+nyvHceqqqhQbeSJa2ADLuA0fJEynU8RxXP/73/+G53kqjuN628x8WzLf7/e1eedU/kNGbNg8M2bT6XQUG/J1U/nrJPL4890fxzTxfBB2cHDgNAcg+jnb398Xgy8SiUQikWj7zDzwFrPhGsebHILlWkpO6dnE20OwbOh52LVlCJYarGZri6Ouw8wrpeom+a/zPL/QaJOmqeIhWOAtclMUBTE/v01mHniL1fT7fWIz7zgO7e3tqSzLqGka0qn8h4rYXMXMbzuR53dTwjCk09NTHBwcIEkSbeSb61n5n1YkEolEIpHo2sw8n24GVJ3GlNzIECwAslEbewjW932zwUZ/nbl4GKgNp/PbWhy1CTM/Go2U53kqiiJ9XYzZTCYTHB4eahMLrE7nsQNmntP5MAzBZt54nG3ziw8ZsVmVzn/55ZcAgJOTE2cXiXyapioMQ2oz8usclYtEIpFIJJJk/trMfK/X08l8WZZ069atGxuCbcy8xoJW1FOaKT3aEnqupNzm4qh1mPksy1Qcx/VkMkGSJNqc2h+xHBmi4XCoF0Y1CfXWmHkegOUkPgxDZZj3C8n8h47Y2Kn8l19+iZOTE+err75SL168cOI4VvP5nHaZyJs6Ozu7cNo80BODLxKJRCKRmPmdMPPNAKvDmM0uh2DZ3DeGXlnpvDbxhrFvNfNlWRKn8ZzW23z7NhZHXcbMc5PNbDZTe3t74BabPM/p+Ph4AX0B/twAG4ahTuV5adQumHnT1JuGPk1TcBrPyfyHhtjwaTOVBwA285zK37t3D7PZjB9jzdNvO5E3zXyapooPCrrdrvrjjz/0MLTjOLUYfJFIJBKJhJnfGTPPRt7zPLrJIVhzcZTneWjMIexWG7PNhg03p8Z5nl9AbabT6bUWR7VtgV2HmTdrKs1Wm8bQOUEQKD6Y6nQ61O/3cdPMPBv5tmSeH5MPFbHhyzx8+FCxmX/48CH9/vvvdOvWLZjPza4S+YODA5yentZ8IOc4Tv37779TFEXq5cuX+PzzzxUAPHr0aOG+i8EXiUQikUiY+a0y867r0t7eHjmOcyNDsI1Rpoafv9A5DwO1wdsWmwtmngdhsyxTJmqTJEmdZdm1F0dtwszDSuYZtTGHYHmDLqM2/A7Ju2LmJ5OJNvOO49TT6RTNVlz1ISM2f/nLX9R0OiXbzPN17+/vw/h+bDuR//3332k8HisA+Pzzz9VPP/2k7t27Bz7wefz4ceuBjRh8kUgkEomEmd8qMx9FkeM4DrF2PQTLRpkTeTb0vP2VP+dUvs3Mm8ujGLXhIdiqqlSWZddeHIU1mfnbt2+rNE01ZsNmnpGkXq+Hqqoc5uY5mY/jGFmWLZj5m2Lm7WT+Q0dsgLfIExtw/hxLUvnZbEbdbldtK5H/6aefFADw7Y3jWL18+VIBwJMnTxQAfPvtt+qf//wnAcCtW7cE0RGJRCKR6CNm5gFcyszTrpn5Tqejh1x3NQRr9s1zQs+Loxojqmsq0zStlVKKcZuqqurRaKTqulZlWdZVVWncpkmiVZqmdVmWSil1rcVRWJLMr2LmzSFYE7Mx03kzmee0vs3M74qZz7JMhWGonjx5gqqquPFGTSYTFQTBR4HY8EfTwKMlkY/jWJ2enmLbifyTJ0/Ut99+qwDgn//8J926daueTqfU6/XUDz/8oL7//nsAwD/+8Q9p0RGJRCKR6ANAbJYZ/W0z8+6OmXllIkLbHILlVhvTzPMQbNNiA9d16/F4rJq2m9pxnIUFUrPZTE2nUxVFUd2G2kyn03obi6M2YeazLKOjoyNkWaZNPJt6TuhN1KbNzPNHTuO3xcwzZgMAT58+vYDZ2Mn8h4rYtBn6ZXhNY87rXSTy0+mUHj16VAPA999/bxt5JTWZIpFIJBJ9GKl8q5nfMTPv7JCZ1/djF0OwnMxnWaYRmeYAoG6MrO6bz/O85kYbTub5uj3PU2dnZ7rB5sWLFwjDsN7W4qirMPOcztuYDSfx5s82zfxNMPPHx8c4Pz9Xz58/h+/7yjTznMx/qIiNaeZNxOazzz5Tf/zxh5rP53R0dAQ28wAwHo/VDhP5ZY+fWnYZMfgikUgkEt28mTeZ+QXEpi2VBxYwG1yBmXfXYOadbTPzbDzsZH6bQ7DGMKxKkkQRUc1981VVKW60AVA3Bwn169evEYZhPZlMeEiW2X2kaaqOjo7UaDTS6XzT1qOuujhqU2bebLRp3ilRjNbw424OwTJyc3p6qg18GIZqMpnsjJl//Pixfnxs1GZTQ/++ITYAwGaef0av11MvXryAncrfUCKv1kntxeCLRCKRSHQziM2mtZTIssw5PDxEWZY0GAwoTVMnjmOyMZtrMPPuEmbe2TUzz5iNyc1fdQi2SZ3ruq7VbDZTWZbVDVKjkiSp7b55ZufZpMZxXLOhvQyz2dbiqHWZ+fPzc9rb21O2kW8bgrW5+aqq1Hg8VrPZDEEQqLOzM/UumfkPFbEB/kzlf/vtN8qyrL53796FVP6mEvnrJAwikUgkEomun8rjEsSGLkFsqOl5Jzby1+iZd5Yw824LM+/sipnnZps8zzm5R57nahtDsADq8XisqynZzNt985PJBOuy83meb21x1HWZ+SAI1OnpKYIgWDkEa26AdRynfv369QJmA+yemf9YEZuvvvpK/fLLLxqvMVP5JpFXANQuE3kx+CKRSCQS7e7vo/11Zwlig2siNuT7PhvGdXrm3UuYeaeFmXcsZp62ycxz0sxpvZnM53mOuq7r6w7BGouo4Pu+KstSM/OXNdpwOs+nHcdxGtOmqym3sThqW8z8fD7XmE3bECwR1aPRSG+A5WSe22+2zczbZn4ymVCSJPTVV18pAB8VYmNWUC5J5d+ZkReDLxKJRCLR5n8LVy2LQhsvn2UZrUBssAZiQ67rUqfT4fYbp2HmdRJ/Sc+808LMOy3MPFnMvLNNZp4NPW9vzfOcN7lqfv66Q7C3b9+uT05OtKH3fV/ZvDysekrGbIC3Q7CczB8eHlKappTnOe3t7S0k89teHHUVZp4xm/F4DB6CTZJEhWGo/vWvf+kNsL/99pvqdDpql8w8ANy/f18b+vF4TIzY8Pnz+ZwGgwF9qIgNADx69KheM5V/Z0ZeDL5IJBKJxMhjLUb+wmVbWmwAtCI28H3faUFsaE3EhhzH0ZhNVVU8JLuqZ55amHlYzDy1MPOERWaets3MV1VV13WtmJlPkoTT6J0PwZ6fn4PNpNlmYw/B7u/vU7fbxXg8vpHFUZsw8zwAy4hJkiTq7OxMm/nJZKJOTk5gGvplS6O2ycybl9/f39doEn9/t9ule/fuER/EfGSIzXtn5sXgi0QikUjM/PJFUW2Dr1gTscE1EZvWWkrm5Ft65skagHVamHnHYubJSubJMvPYFjNvYzYWM49tDcFiySbYIAjUYDBwAFyop9zGEOw2FkctY+abg4L67OwMtplfdwh2XdTmqsz8nTt38OrVK4cxGwA4PDzEH3/8od/dms/n9BEhNupj+wUoEolEItHHgNdctigKlyA2sFpssASxQRiGjoXYtNVSkoHYOHYtJRFRHMcwzXxLz/wFfMhi5sli5sli5sli5nETzLzneTsdgr17967m5nkItqoqJ45j7GII9qqLoy5j5vn+2sm8PQSLNdP5bTLzAHBwcKCT/IODAwhiIwZfJBKJRKJdJfKtpr5BbABcWBQFA7EhXhTVIDawWmxgITbU1mLToByM2Djckb4CscEyxIZT92YxUlvPPBnMPFnMPBnMPCxmHsAFZh7bZOYZ1YnjuJ5Op9rI38QQLKM1JmpTFIWzyyHYdRZHbcLMAxcxm6sOwQLbZeaxiOhgmYkXxEYMvkgkEolEV03k9edffPEF8jwnq8UGwFLEBlaLDfGiqAaxARYRG7oEsYGF2OhBWHP76zJenhEb+4CFDbzVZGMy89TCzMNg5mEx82hh5rFNZj5JEnV4eKg6nc5KXh4fyRDsssVRu2bm7YVRP/7444KZvwlm3k7mAaDb7QpiIwZfJBKJRPL3ZyO8BmwWHzx4gCzLNC/fGDkaDodoEBuyFkWR0WJjDr6Ch18bxIbWRGxoBWLjmEOuGyI2nOLD5s3ZsBjMPCxmnixmHgYzjxZmXt0kM/+xDMFua3HUpsm8OQRrojZtZh7YDTNvYzZpmqrXr18jjmMFAK9evRLERgy+SCQSiSSRv2jmv/jiCz3YV1UV7t27R0VRcGJJvV6PU1kehGVmHb7vcx0ljEVRWIHYoAWxoS0hNm21lFdCbIqiQFmW5Lpu3TDzMI18CzOvDGZeLWHmsUtmns38cDjUX/9Qh2CvuzgKGzbaoGUI9sGDBwCgTbm9NOqTTz6BidkA22fmu92uevPmDRzHqc1k/vnz5zg7O1OmmRfERgy+SCQSif4D8Rpz4LVJ5PXQa1mWqKqKhsMhDQYDpGlK9+/fp/l8zhtf0ev1kCSJEwQB7e/vowWxIaPFhpYgNtigxWYbiI3ZZGMjNjC65jVe07xLgaIoUNd1rZSCwcyTxcwri5nHEmYe22Tm9/b2AAB5npPRarNyE+yHOAS768VRbOajKFIPHz5cGIK9e/euNvjHx8fo9XoLZj6OY7p79y49e/ZsZ8x8FEWq1+spE7M5OTlRfF9NMy+IjRh8kUgkEn24f0Mu3f5qJfJ07949FEVBbG7aEvk4jilJEur3+2C8xnVdTszR7XYdHkJdsigKV0BswL30GyA2Zkp/VcQGVi0ljI551HVdF0WBqqpUnucmMw+DmUcLMw+0M/Nq28w8ozb83DQHBB/dEOx1Fkddxswbi5/ozp07eP78OZrXl/OXv/yFl3w5wFvUhv9PMTO/v79/IZHfJTP/66+/4uuvv1b/+te/VPP/VjXJvCA2YvBFIpFI9BFsegWwkMjDZuR50NVI5J379+9jSSIPs1eezXiDcFBZlmDExnVdsHG/AmKj0Z5rIDam0V+F2FBZlmhBbGDUUpJp5hn/mM/nKssyNvOwmHm0MPNYwcyrXTDzZjIvQ7BXY+b7/T7x97GRZwPPPfF3795dOgCLG2bmAeCHH37A999/r/7xj3/QJcm7mHkx+CKRSCR6nwdflyTyaEnkneZzLEnktcHudrvUJPILvfLdbhdRFJHv+1TXtRMEARixISLwoqiqqpxOp4NOp0OO45jtNZsiNvbmV7R1yxsGni5BbPQm2AaxUQ1GAqOWEiZikySJqusaeZ7XdV2r2WymqqqC1TOPFmaeLmHm6SaYedPU/ycMwW6LmecDKTbjZj3lsiHYs7Mz7O/va0P/Dph509STIDZi8EUikUj0nrfXtLTWMCMPK5GnhpFvS+SpYeTbEnk0iTwxI2+lvOS6LvV6PTTmnjqdDpo0XnekcyrP9Ze+71Mcx1dFbGwzvylio8Nmo4pStSA2MGopYdRSqtlspsqyVL7vI0mS2uDE0cLMYwNmnnbFzLOR5yMo/QOb0x/LEOwmi6M2ZeYNRAfLhmBnsxk3JeHs7AxRFKnT01OdyPPjcoPMvCTzYvBFIpFIdAO/19WS3/NLWfmW1prLGPl1E3k0ibxm5Fck8twlz4k8IzXodDpUVZU27mziGbVhH2mk8uDzliA2MBP6DREbZWx+RUuLjVqB2Ji1lMqopVQGYoOiKNRsNquNJps2Zh5XYOYdYDfMvGno+bHhpN5O5j/UIVhgs8VR12XmTdSGP3766acqy7IFbn46nerL22b+Bph5EmMvBl8kEolEN5vEwxx05WVQAHDnzh16+vQpjo+PYbXWoCWRRxRFZCXyWMLIw0rkFxh5I5HXjLyRyBMA6nQ6MBP5KIpARNqE8u0xjbzZWmO12FwFsSFGbQzEpq3FRi1BbNCcV9d1jQaxsWspTcQGdi1lWZZqMploA+/7vjIMPF2DmXduipn/2IZgseHiKGBzZj6OY3V6eqq/nxttzOR/Op0qRm0ePXqEhw8fqsFgoB4/fozHjx8LMy9/IEQikUj0Af+epjYDz6nznTt3wDWMbOoZqzk8PERZljrBDYKAViTyAFoZeU7kKYoi+L7vNKa9LZFHw8hzIo+qqhzf99GYeN0nb3Hyyhx65fscRRGWpfLLHr8WxAae5+kU3kJsePAVFmJT53lOFmKj7Bab2WyGBrHhxL5unguzllJZtZQwsZpLBmDpmsy8s2tmvi2ZBz7sIdhtLI66jJk/PT2t4zgmE7NZNQTL7058/fXXan9/X5lLo4SZlz8cIpFIJHr/kJpl3fHasN65cwdPnz7FwcEBJ9QEvE1yy7LEYDDQhr/X61FRFBgOh2xuKAgCAIDruk5LIo81Enm4ruu0JPJYxcg3iTy3r/DXViXyMDa+thr6Bq9hVAVJkqDBXfTAq4XY2C022ACxQdNig0sQG3Aq7/s+ltRS4jLEhp/TFmbeuQYz79wEM8+Py3Q6xYc+BAtsvjhqU2aezTxjNvw4sJkH/uTmHz16hF6vpwDghx9+IAD1EjMPSebF4ItEIpFou79v1TUSeW3kuW6SUZO9vT0aDAZmcw2KotAmmpN4vvzBwQGVZYnhcEjz+XwZIw8jkTcZeXCXPC+GWsLII45jahj5bSbyC8ujkiQh3/f1dlezQpHNMg9itiA2agPERjUtNuBu+SiKlIXYwNr8yu8S1A1iA1xEbFrvI6fywFtuPs9zGgwGMN9pAS5l5p01mXm6CWaek/mPYQgWV1gcdR1m/smTJwoAVg3BXsHMCzMvBl8kEolEOzD0ZBp4A6nRabuJ1PCQa1VV6PV66PV6lKYpRVGEW7duaX6eDXwcxzqQZYyATTyb6iAIsCKRp16vR4zbtDDylyXyqxh5soZabS7+0kQefw6+IkkSuK6ryrKEUmohredk3lgURSsQG7vFxkZslGlasyzDihYbYL2OebIRm+FwiDRNdZMNYzZXZObpEmaedsXMs5k/OzvDbDZbMPMf0xDsOoujgKsz89PplABgzSFYSeZFYvBFIpFo14n8EiZe10waQ5/UhtQEQUDD4RBFUcD3fQqCAE2NIddEcnOM3ubKH7mbvCgKxHFMruui0+lojMZI5MELoZpEHpzKt7TWXCeRX7ocq0nk4fu+PlhpkmOYg7BGIg+jV55c11Wz2QzqrZDnuWrqKInxDwuxgdVigyWIjVrSYoMliI0+vQFic6GWkitBG8OvjbvFzNOazDxWMPPYJjPP71q8fv0aADAYDOqXL1/iYxuCve7iqCsw8+um85LMi8Tgi0Qi0XV/T64w8AsojcHE66FWk4lvkBYsQ2p836dOp8NpPIVhiOFwSEopsOlzXZccxyHHcdAy5MopPHijKxHpLnn+eEkiT0YCvA4jf+Ex5MuyETYSeW6tMRN5YrTGSuRR13U9m83IdV09BFtVlWbDy7LkpBlEpIwWG1yC2GBFiw21IDYX7ud1EZssy+jo6IgP/pwoisCmfknPPF3CzGMFM083zcx/DEOwmyyO2hIzL0OwIjH4IpFItK3fhRZCQ5aBxxpDrSYTv9TAx3GssZY4jjkJRxiG1KAYcF2XOp0OgiAg3/fB+EVVVdTr9chxHBCRbr1phlyJiMCbXHmrKxt0IoLneewPze2uuEIibzPynMibjLxOGjmBtxJ5FEUBI5HXw6+8IKqua9icPLfXVFWFZsMpyrJUZVmiMaw2YoOWFhtYi6JoDcQGLYgNNZjLtRAbHoI1U3m098zTCmYeK5h57JqZvypm874PwV53cZQMwIrE4ItEItEOf++1MPDLEJrLEnhzqFUb5IODA7QZeDbBbUiN4zhgQ+84zgJLTUTkeR54m2vTbINut6sHXM2lT02lJdjAW3w8GrRGd8Q3A7frttZofn5NRp4MRl4VRUG8IIqHWI1EHusk8kSkmrRcAVBJkqAoCgwGAzUajS6k8laLDQzExlwUdVOITWstpbk0iogoDEON1vDBl8XM0xJmnlYw8/S+MPMf+hDsNRdHiZkXicEXiUSiTX/HrTDwyxAaRinISOBthKbNwFNT9XiZgdeJPifydV1rrIaZeEZtiIiCIIDv+2BGPo5jENECF+95HqqqIs/zWg08G3Vm3FuM/DqJfFtrjZ3I6zKblkSeDEbeTORVlmVYkcgzOqJT+SRJlFKqBkCj0UiFYahmsxmyLFPmgGuSJLAWRa3dLW8gNmgQGwKAplaUut0u7QKxOT8/p729PWUYeVrRM09LmHkFLGXm1U0y822m/mMaggWutThKmHmRGHyRSCTCCv4dAJYgNLqxxDDw6yI0KMuSmgR+lYFHt9ulxsBfGGp1HIcaA28y8Rf4eP6agdSYifxCQs/mktN528Cvufyp7XN1SSJPl7XWNAn7pok8liTyajAYYDQaKT5Y43pLTuTLslSDwQDj8Rhs2Ph2c+qO9RGby1ps0O12F9CpJYgNOY5DWZZpM7opYhMEAU5PTxEEgd0zjxZmXq3BzOMmmfkff/wRX375JU5OTpyPfAhWFkeJxOCLRCLRmsy7/npVVeCFL5y8L0FoTANPRgLPBp6MBH6bBv5CAt+YdfO0/ppl4DWOwcm6uXTIPJ9vl+/7ZDS2bLLNdcHA8+InPkBoTDXxcih+jJthV2pprbEZedowkefLKTa4o9EIyxL5k5MTfXo+n8PzPLW3twdOrk3leU62uV+C2GDHiI3iYdcNEBuyl0ZxKt/CzGMNZl7dFDMPAGzmGWkxufmPdAhWUBuRGHyRSCTm3U7cm02s+rJs2quqosPDQ05t9R/8Zsso4jhGUyPZhtDYQ6xtBl6beKtW0kRocMUEHsZQ6zIDr9Gey5Cay5Y/md3xpvlaZuDN50EpxSz9QipvtNYwIw+jtUabq7Is0STy1CTyakUibzLyayfyAHB0dITz83MzXV5Idtn4ZVlGe3t7aDbTbtJigysgNubmV7QgNvp2roHYUEstpU7lx+MxLGZercHM4yaZ+YcPHyo28w8fPqTff/+d2Mx/xEOwgtqIxOCLRKKPz7BfkrjjssSdz+PEnesazeU8ZVky/6w56GYJk06ih8PhQn1ji4E3GXh0Oh1qDLydwGvTvqaB1zjNrg08rAFX810N+3loMfAaBWEzz/8YsWFTb7XWtCXyurHGaq2BlcibjDyumsiziW9Sd9UYYTJTeWazB4OB2WKDS1ps0HTKw0BseFmU2S1PSxCbhTrKNRAbWLWUaKml1E02jN8YzLxag5mnd8XMm402cRwT40lJksgQrEgkBl8kEn0oht027fx5VVWrEnfNvIdhqD/vdru6IYavvxkqZSOoW13CMNQGvklksQZCsyyBX4XQ2AZ+IbxtDLxZL7nKwF+G0bT+bl6C1FzAaWykhoda2czneU6M1NR1jaIo0FQpana+rmtMJhM4jsOMPFoSebQw8uCUtdkce1kiT0dHR6pJ5FvNvJ3IM7POP4MHovnAr9PpUL/fR5qmxO/U3ABigyAIsCFio1pqKbkytD47O8PZ2Rn6/X4NgCaTyTrMPN4HZp7fiTPnDmwjL0OwIpEYfJFI9J4a9l6vp0/bpn1J4o6WxJ0TUTbsGh8xv4+TVtd19deiKDLbYhCGITfPXJbA6+FUi4G3ayV3YeBbP2ee3mbii6JQ3Edvp/FGU41eCFXXtU7hmZM3DLxedtR0pKsmTQZveE3TlIweebu1po2RX+iMX5LIk4nZLEnkwaa9LZEPgkDXkwLA4eEhpWm6MKOwt7fH6JU2ofy8bYjY6NtgGnkAZCE2sPrlYW1+JW6xMVP5NRAbZbz+dZUlG3aLmceHwMzLEKxIJAZfJBK1GHXmoZeZdQA3atg5bXddF57nYUXijpbEHS2Ju2bey7IE4xJRFFGn04GBTwCAximCIFhAZi5h4Jf1wl/HwF+G1Kw08GwiLSaelFKKN74y+mEiNbPZDJ7nqTzPKc9z1STmSNMUruuqxsizAdUGvqoqpGl6Aanh14e1OdZsrbkskScA2GUiz9/HeA3wdtFRlmWcxgMAua6LJYiNfm42QGzU6ekprUBssKzFxkzl10BsYCI2APDs2TNl4jUGM4/3nZlfZeZlCFYkEoMvEv3HGHTXdRcGTo+Pjy9czjTqsFpBbtqwmzWEbNp5uJWNeEviDitxh5G426Z9IWTtdrsoikKjFGze+TZxWhvHMbZs4LEOUnMFA78w1GobeAOp0SiNYbSRpimUUhqj4ee/KAqVpinyPEeTcJvbXHUCza9FNrmMlZgd6r7voyWRR8PIq7bHpDHhsBJ5Ml//SxJ5pGlKSxJ528gTvy45lefXFyM2zOdbiI3ig4lLEBsAgF1JyYOvFmIDc/MrP6YGYqMAoAWxAXDlWkp635n5JEnUZ599pv744w81n8/p6OhIhmBFIjH4ItH7bchtbcugN4keLPZ6waizsTGv710Ydj6P2XY258yzW4k7bPNuJe4LyIxp8gyDbg5KLuAzJopx0wiNZeCpwTE2MfDERt7zPJVlmTbnvu8zUsMJPC+IYiwEVVVhNpvpJhvjZ6qiKOB5HvPf8H0f0+kUcRyryWSib7v52NjDrVtI5M3lUOsm8mjrlTfxGj5AZFbefu74cufn53ogl/GmJYgNViyKsltscAXEBksQm7WbbAxmHkmSwGDm6eTkhL766iu8ePFi4eBzf38f/X7fuWlmvtfrKTOZBwAZghWJxOCLRO+tITevg+sbt2nQjRaZBaPOHzudDsbjMUyjw1gLb1W9acPO+Iv50UjcYSTusBJ3GIn7gqG3f0fxdlbGfTzP00OnptFfE6OhNYdY2wx8a60kX0dbAm8YeKwy8E2bj8qyTBt6xmlsJp4T/GbDLA/IqtlsRlmWqTiOwSaeERI28OaMg5Ua0xJG3kzkzdaaTRN5/bi2oDW6yaYlkUeWZTCGXvX9b/vbFgSBGo1GulseALEBn8/nZFZSrlgUpdZEbFRbiw0APHv2DEsQm41bbLbEzDvAu2Hm+f7LEKxIJAZf9BEZ6stkMuRPnz4FAG24V6ntZ27LkLNx4iHQVT9rGwadja1Z8QijDtE06nZaz6b7XRv2lsT9wvmmuV+RuC/gNS0J/MYc/DUMPF2G0Gxo4GEaeIuJX+DizY28eZ7D933FP4dRkQat0TjN4eEhxuMx8jxXvKW10+mg0+ngzZs3+v/Amzdv9GuXX3/dblc/Zuawq5HIk9Elv24iDxutYUaeX0v8fDWJfOvzaSXyJlqjAJDVLY+qqvRjYyA2q1psaAPEBttqsWkz9KaZj6JIPXz4cJvMPO2amf/qq6/UL7/8soDZAIAMwYpEN2Twv/nmm61eoe/7OkngVNRMDEXb01UN8XW1iaFe5z6Yhpuvy9hC2moe226L2XixbUNuLkla1s3tOA5vF72yQeeqyCAI9Hn8kRN6/lmO48D3fW3Ke70e5vM5Op3OjRt2+zJ8/8zP+aNh3nGJYV/bwPP179DA0zUNPDfTLBtqRRsTz0iN2VIzmUxM/EP3nJupfJvJbFJkMg0f/wzD0PPeAQrDEBsk8rq1xkZrDEYeALiGErxMqqWGUrU930EQYDQaaU6+wYr0fWdD3yT0C4Ov5+fnWNJigw0QGyxpscEVEZuVtZRxHCtm5u/cuYPnz5/z4+xsgZmnd8HMA5AhWJHopgz+f//3f2/V9PEf7LOzM839sjgtEsN/PVN/HUO8TXO/rqG+TG2Gmw3pssuay33sKsWbMORtP/+6Br3T6aCqKs2lM2vd6/X09/HlzQScL8/G3Xwn4KYNu3k9zKtf511FXtxkJvrW424beGpZ7EQrGHjaMIGnuq6VYeCppZUGmxr4honHCiZe8f1ctQCq7TGM41j/32Be3nVd7obXJrtZCqUTeX7c9/f3yUjksaS1ZlNGXpt9m5HnYVdzCNbm5C9L5M32H7OSsjkwWBexAdDaYoObQGz6/T4lSYIoinD79m3i5zUMQ9oCM493zMzLEKxItGuD/z//8z87ueKmvk3/QZhMJgvmSXR1bWKIt/WztmGo267PfE0YlYithvsy7dqQB0GgL2slo8jzXDfBXMegt5l08zJcJWkbaNusWx3u79KwL32umF2/JHHXhp3PZ+acH7+iKBb64RsDT5cgNHRNhOYyAw/DwKs1DLzNxC+w8XmeE3/NROJMU2cNuHJPPZkImonX8PX1+32Mx2Pqdrto6ZGHXT/Jp9taa6xEfhUjz4m8ZuSNRJ6MRF4FQQB72+uSRF6b/SRJVBiGMFJ5GIgNgAuLonBJiw2ugdhQFEVoEBtaF7HhVJ7NvJnMM2Yzm83Az9sGzDy9B8y8SCTatcH/3//9350YQv5Dy2+L23+U+LJi+jcz2Ns0xNswaZsYavv72gw3DIbcPpAwrzvLsgvvBPHjsQtDbqfgpnm2je91DLr5M9ik8+Xs59/8Htuwm4/1TRt2K3G/8DyvqI2EUorKsjQN74VE3sRp+OMaCA3t0MDbrTSUJIlax8Cz4TeZePsdOubigyDAixcv9MDr3t4e8jynMAyXGXjiEKB5HAh4i/ExluN5Hrmuq4cwXddta61ZxshfeLfgEkZem1+LkedE3t70SvP5vDZeA3Yir4dez87OqNn2qh9XZuXbuuWjKELLoqi1jfwKxEa32FwXsTG+f1nPPG3IzJMw8yLRf5DB/7//+79rX0mWZfqPqin+Y8gJWZshEK1vpK9qiLfx869jqC+Tabj5czbJZoKONeY/+LLbMuT2da/62bsw6PbzsMys7+I1d13DbppjK3FHS+LeisxwGm92wPOWVk772bQvMfC0RQO/SQLPvLwyDDwtM/DAWyb+9PR0YdB1f39f/+5szCdZ24B5uBW+71Ov10O328Xp6alGajiBb5aILSwIY1SHjbzJyPPyMHPDcEsiTyan3iTylzHybYm8WUPZlsgjDEPm5LVR/+2339DpdNTx8TF+/vlnfVAVBIF6/Pgx/v73v8NO5Vt4ebrEzMNCbOjk5ISAtRAbiqJI3b592zGQpSshNqaZbz7SBsw8hJkXif7DDP7/+3//b2uGgNMz+4+k8QdZtAVtyxBjCwPV6xhqrMH18/dxT3un07lgdm0za88bXNeQm0a2DWWx31Gxr8/8vndt0N+1YTf7xk3+nY277/vI87wtcSff9/USJ/79UVUV8jwnZq85VGDz6Ps+3bCB3yiBbww8GQZ+KRPf1h2fZRn6/T76/T7xuxIAaG9vzxysJbNmcomB14aWD8oXNonhAppCjMRYifwyRn6t1hreFbAkkV9orrHRGh545dPmwCsAYjPPRn6NFhtaYeY3abGBgdggjmNcgtiwgccyxCaOY3V6eqoP/D777DNsyMxDmHmR6D/U4P/000/Y5vAl/6Fv6tzE2GM3g65XMcTbHqS2DbVtXjc9WDAZ8mUHE6vQkk0M+aoUvQ1luWlDvs5zb9+ud2nY7fM4cTcP+Ou6XtjK2ra4KcsyVFVFZvc73y7jfpHv+0iSZOGghX+2efvWNPBkDrGaJtEw8LSGgYdl4Mkw8LAMfGvVJD+XeZ5r49fr9VBVFfH1NkgNXNel5rGjO3fuIMsyNvJ6wdRlBn5Vgr2EkcclrTUrE3k282ykkyRRKxJ5/Q5AEARoEvmtVVG2ITaTyYSYlY/jWNdK7gixAQBahdicnp7WzQGb4lR+DWYewsyLRCLiX1q7qk/kP/LX7UgXXd0Qb9NUXtdQr3u/rvL9qwx52+1a9TM2uQ2boGZXMejmgfOy2/e+GHYrcYeZuPNp8x8va/I8D2maoixLHnwmPiApiuJCgm5XSLLp32ECTy0G/rIEno0yWXsOKM9z2AaeP4/jGGVZcl0lHMfRjDybeB5s5VTexDw2NPB6KRQvgzIYedqwtQacyA+HQzUajVoT+QZbojZG/vnz53jw4AGePXum7OeKDfyqBpterwdeUtVm5gHg/v372og/ePBgYXOvycv3+33q9Xo0nU7V7du3ndevX+Po6GibiA2SJMEqxGY8HuPzzz/XtZQ8HNzCzEOYeZFItPCLv/kFj10Mh4qxvzmj/y6GlVcZVSK69DbZt3tTQ23PceziMbisf98eKl9m+tmoLjsAsg1626yKOWhqHizY757ZXLtp3Hdt2M203X4M+GCDbyO/y8fpLz/ebNB4OJbvG5vxNqxqNBotNfBlWRLvh2AD32bklxh42iCBv5KB5554fkyjKMLBwQGyLCObhTeHQPmx4QFRpRSIaOH1o5RClmVkpeb8OVVVpRn3IAj087CkR35lIs8YD7fW8PxBkiSKg6Rer6cAcBoPABeGXa8is4oyiiL117/+lSaTyQJ68+DBAxwcHNDTp0+1kZ9Op8TPERve+XyujTxjYmEYthp5G7GZzWaU57kyKko3QWwALK+lfPLkiQIAg5lXwsyLRKKlBv8mEJq2P/TSnnO9g6abbMzZ1FBfB9W56rsRu+7/X2Xw2ZyzgTafK/txsNP0toMDvh7zcibyZibsDX+uWXbTtPPXlFK6BnJXht3zvIXbkmUZZrPZhdcJs/OTyUSb1dFoBN/3tXm2zDotW9wEozHl8PAQeZ7r0y0GnjhxNZc7mQhNs7CIWhL4NoTGNPBUVRU2MfAmSkNEeoCWSRq7O94w7szlKzb4SimVJAkxG9+YddUk7Po+T6dTfV9spGY2m6EoChUEgT7AMhP5VYw8H3idn5/rOkpeCsXPMZtTs22nSZgBAN99993S55dNuK3RaERff/01fvzxR9y/f18/VHwwN5/PeVCZ5vM5jo+PkSQJ3bp1C/P5nOI4pjiOFRt33vRqmnhm7PM8V/x5SyJPppk39fTpU3V0dESO49TT6ZRaainVmrWUip+XR48eKWHmRSLRpQZ/WUfxthAdEd5Zsn4TBvddM+l4Rxy8marP5/MF/rstaTeTdNOo2+k7m3Dz8pyss3ho3URUTPNuGnX+Gu+lyLJsa4ad03Y27DxEPBqNEEUReCjSfJ2Mx2P9/bz4zjTiR0dHaGviMisfVynPc+zv78OslGTTbibgNkLDQ6zA2174NE2xv79P0+mUE3jN1TcGXpv3VQm8beDTNEUURdrAc+VkY4wpDEN9+8IwvNAhf35+rk39fD7XB27GP8UmdTKZKPsgrSgKNop6yNU84DFba1oSeSxL5A2Ehkwz38x4qW+++YZMQ79MptG3jT3z8nxQcXx8TPP5nO7du4dffvkFd+/e1Uu4+Hus9hpt4tms7+3t6frQ0WiE4XCIxsCT+fo9Pz9Ht9tVnNDbrXBpmi7ct5OTE5UkCbGZ/+mnnxTwduv46ekpNqilxDXMvEgkEoMvEok2Nfds7PM8R1EUmM1myLJsAYExU2s232YKb+IxPHTKZpiNO5s0/tgMoi4Yeb5NzLPbQ6jmOwFFUSy00JjG3UzYTcPOH82qVN/3tWEvy1Kfb1eBAsDLly/BDPHTp08XrqeqKn1eURSabWYznuf5gsHvdru6WcZcqhcEgfk9C/3wnCyzkedKSX68OX33PI+yLNMHGlmWIY5j9Hq9pQgN8OfiITuBNw17EAQIw1B/D5/H5t4ajCV73iBJEq6pVLPZjA/c1Hw+x3Q61a+F0WikuLKWWe88z1WWZZzGwxxy5dswm83AnLnVWoPXr18rs/721atXC3842MAukVp3gzEb/FevXunNrffv39ccfb/fp3v37hG/Znno9datWzA393IqH4YhdTod4u2/w+GQPM/Tr4vZbEaDwUD/fxqPx3xApPj1yOb+7OyMjTyZhv7k5KRuWH28fPkScRyrs7Mzxan+y5cv+cBD/fjjj/j222/Vo0eP9MERm/k1ERth5kUikRh8kWhXBp+T+2boEFmWIU1TfdqsD+REtC1lN3dI8Omqqhb2SphpO5/2PE//DHMQ1R5s5+vl6+bhUzbtjG7AwDjYfHuehzdv3lww7a9evQIA3L17F77v4+nTpwDe7mMw39Exqgsv6JtvvoHJR+d5jk8//RTT6RQHBwc4Pj7GeDxGnud6KNJm0Hlb63A4NA283tza6XRgdMTrpHY4HCKOY23eXdclHl7lr3e7Xd0Hbxp4Tm552ZRp2BsDr2+DZeABgLIsw3A4NPeC2PiJ3gBrmnvun0+ShA29mk6nmE6nyPNclWWpOXnHcXSNJA+y8muFG2sahAZm9eTz589h9MjroVaDkddG/rvvvsMPP/xwlT8eS7vnv/vuO5pOp/TXv/4VADCZTKhJ6XW//Hw+p+PjY2bmwe01Jh/f6XTQ6XTo/v37mM1mxAPQg8FAD0M7jgNGeSaTif4/0BwkqdPTU23wT05O1NnZGY6OjtSbN28wn8/VdDpVn3zyCV68eIHJZIJut6sMZl7jS2YlJf7k5bFi0HWdhF4kEonE4ItEuzD48/kceZ5jPp/j9PQUZVkiSRJMJhNwxWOWZTo5Nzl3Pt9EZoA/03cbn+EEntN+08Cb6I2xnVSfn6apNv5Zli1UNrKpuXXrFl69eqUTewArjXtj9Lb2mH7zzTdIkgSz2Qz/9V//pbe09vt9eJ6nERsTr2HDNhgMdCoN/FlnyYls0xPP21r1wrYoihY2tbLB39/fJ3t5GwDdwW8O2RqGfuEAo2XRWxtDrtP9VZdloz8ajTSW8+bNG5RlqU5OTrS5f/XqleJ3kLIsU6PRSFdYnp6eIggC/PHHH+r4+BgvX77kVF7XUwLAs2fPYM87bGDo24y7WrfZ57vvviNO7vmAjs39/v4+MXZjGvvDw0MEQUDz+Ry3b98m6/nW23hd19Xfx7sFTNaeD7in0ymKolBnZ2eqLEvF5p4PlLgxaDqdqslkgn//+9+IokjFcWzz8/rgiI39isdOzLxIJNqJ/j+au6yDLchSYQAAAABJRU5ErkJggg=="),
    "check_off": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABjElEQVR42s2UzWoTYRSGn3O+cZohdFLpFLuRIMFKNvUqCpUudNGbcNkL0EV7AV16Ey50IRZ6FXYTrAQJblo6YjMlTDqd7zsuTCF0MWDNwgdeOBw4P7xwjjDDzFREAsDpaZ5WeRQVFMyTkhJndb2xkRV3a2Q+cfxhuEOIXlf1zXMLITYLMt9IRE1Uqzh68AWt32296n26rZXb4Oj91/12++Gb7FFCZ1VxTmdj5jDwPjD+GcjPSyaTXwfbu8/empkKwPHH79utuP358dO47vY6AI5m/Gg45se3KppWkxdbL58cKcC0rPay9cS6vQ5mRGYmzSLq9jpk64lNy2oPQAeDi2ULtrmSOfmziSEijQIDcCuZEwu2ORhcLGtRXMciOFXhb1EVRHBFcR2r98H4R7wPpiyI/7CRc/dw+Q7OqWiaLlVm+HAPz0MwzPBpulRpv792JSonl7k3wINgZo2a3Y6/zL2Jykm/v3alAK0kPszPShkNx4hQi4g1i3o0HJOfldJK4kOAxR7tQt7Ioh7bb/euKCsSA5CEAAAAAElFTkSuQmCC"),
    "check_off_disabled": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABVklEQVR42s2UTWocMRCFv6pud2OwNZB9Vtk71zD4JF76Cr6AlzlJDhJvTRhwlo3DtNq4p3+kl4Unw+SHcbBnkQcPhJAe0kdVGRtJcjPLm3VoW0piC2FzIAJhwWLBbGbx9zu2u/Ht6/eLoqwvp3n6qJyqnGXsyN1kXoxH5dGXNA+f3n9493kbJskBlnfN9arJ6jtp/ZQ09PmvXj8l9Z20arKWd831z4cAcL98OF81WV27niRNkvILnrp2Pa2arPvlwzmAA6SRq/rYdBJqgBJhew3lSaipj01p5ArAJZ1mdGaeDSjQhtw+C4DCPFtGZ5JOPcZYGRRmv3D9J5kZBkWMsXJJ4o3SlvYB9B8G2Wso/wndPIQwCtJrmEtCkEIIo5tZ59itsgtI2zrZ5+c/JGWXY7fPGUBRcTP0ssc4AMwv1LWA+TEODL2sqLhht+He2rQHGyN2qMH2Az9ZcZgsQp2kAAAAAElFTkSuQmCC"),
    "check_on": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACw0lEQVR42q2UT4hVVRzHP79zzr13mnfffY5TRINFEbgI1Fy0cFGuLCMQWpTh5EbKKBAU27jIxzQUtRARDDGq1TgEuYvoRRBMLYQWwezctDKUFtPzvef1ef+c83Nxx/Gl6Mof/OD8+374nd+Xc4T16HbVLCxIAJjfq9lUGDiGQ8iy5sD6+Jbp1Bd6MrxXI5ML8y9fe8O45EPVaoeqjxUVJkIQFbGlSLQa6uLchT+e+umOVu4MDrxy5dMkmv1E8Xh/i4eFs1OApajWFpd/f/pkt6tGAN7dfXVv5Db9XFaDGgII9kEQEdCAF2uITMdV9fXXl1bmegYgwDEI2kDENcfvTzEiZYGEIK4Ye5CgjRbMoX3aRnV77cfysEqsg2E/sOfNFp99+zgv7Ixtnt8UY3T7oX3aNlU+jCcBxjZ5L2Q0UF7clXDwSMZzWyO2boupCkUEW+XD2MRWdVJ0YxC4MQiIuQse58qWZx1HF2dIpoTexZwfl3PSjsF7iK2qmYR4D2+/n7H/cEY+UqyDqoTp1PDxFzPMzFou/Tbm6y8HTdcmnQQwAlWlPLnF8s4HbQDqWvn+/Ii0LRz/fDPPPB9xebXk7MJ1HpsWjFUm7+Kg8SuOhX//8fzwzYi33msz/1GGc8LmJyzbXkq4dqXm1In/qGslSYQQGi83QKVvpqpNU5e+GjK+qRw8krH/cFNdPgqcOtGnvxZopYL3/zej9CImamUlysbWplnDxe9GnF3ow3rpZ072+ftySat9PwTFR62sFIADu6/+krhsT1ENvIg4Yxv3du6awhj461JBqy2ESYhqHUcdW9TDX5dX5l5rmg2nwbwqGNBQB49NM2H1z+bNTaeCr/VuTxQPBjDSaMF0u2qWVuZ6RbW2GEcdF0WZMxILGkuaJpK2ExGNxZpYjDQZRZmLo44rqrXFpZW53sajfSTfyKP62G4DGUJX/WcbpL8AAAAASUVORK5CYII="),
    "check_on_disabled": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACcklEQVR42q2Uy25URxCGv6o+58wxZmZs7gaDlUhY8gohFvAIIJ6EJQ+QLMIDsMyTROQRYEGisEEacbUNGPkynmPDzJzprspixmTGISgLfqmlVnXV3393XYQJ3F1FxAA6nZ1WWWdZVVXQmjhU0Gq1GBQxrq6eqY7HyLThz8cbd4uyvJfi6FqyVGAuTEPFg4Y6ZPlf9WDw6/Vbl387ipWjzdPHb39ZbJ/6KVliOBzgPrllCg6IQKNREjTQ7e09uHFr5Wd3VwF4+mT99mJr8dHBYS+aGe4E/gsCAkk10DzZyrpV986Nm1d+1/GZ3E+W3MwAyeQbcEPcJYsxYp5ckPsA8vz5drO/3+8URXGhHtUuIvJVIQKjkXF+qcnpc/Nsvun6sO8SU9yaa5er6j4sRP55ish4HSeJ0WgtlFxaWeDEfMF8s4GZo0JwHxZqZj4dFKMRo82QpOSUczk/XD2NqrC9dcjH9xVZrrg7ZuY6kxWHpeU2F5fbpGSIgBmEoPy4eoY8D3R3P7P+eu9fqrOjTJg5jTJw8XIbAHPnw0aPkI1J5k7kHB4MeftyjxAUx8f1MEPkoEEYDiIfNnssLbe5dGUBFSEvAs12yXAQedXZwc3RILgdU6SqAgYOIsK79R4pOcsrCyxN1KVovOrsEGsjZIL7LImqioo0anfSkTHPla3Nijcvdr84vn6xy+fD+qsk7iSRRp2trZ09+OPJxrNGWZ4fxTq5k+WFsrv9idHIEKC3P5hkaKYkUqMsQx1Hz9bWzh7o+Iv8YdAgqgp4NHMPQbza73tvv+8h4Gb+BeBRVQkaxPGHAN9uWpnq1v/TtN9ljHyvwfY3mH2LMFPDTUoAAAAASUVORK5CYII="),
    "radio_off": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACNElEQVR42q2UsU9TURTGv3PusxVToUClYAQFg4MGE0lchIGYuLzBf8GNjdG/wlFY2FzfaEgXEsMALhJISmJCiVXAAMWi75XG9rV993OAYmtCZOgvOcO5uefec8+53wFaICie55mmLwJks35vNuv3ivzd53meISitsRcOSRERAsDy+y9P2dBXUcM+i2x0GwCMmgPj6Edx7LsXL+9/+jdGWhcymVzcqcXfKGTuVjqJnj6D6zfO7qr+JoKfEX4UfFjwbSMWvnbdB2Ez1iEpACSXYyyfzWf6Usnn6buwd+7dtAC0JWsCsN+/UQu7mCsWi49yOboA6iQBz6MBgCVve359JWDJr4Y8x1rbZk1KfjVcXwm45G3Pn9WMRgBgJfP1SaNmNh5O9jaGhhPGWopqWy0vsJZQFR7ul6PPG78cJxZNzrijmwoA5VJtdmAwiaHhBEhceggAqApIyNBwAgODSZRLtVkAUJJiiamefoOzmhD/hwCgPf0GlpgiKbq1upcUMB3v0rbvcAUk3qUQML21updUdAidmB7xCSmEFXuR8xVhWLEgpDAxPeKriFAFa8FJBAD2aq8TALDBSQQVrIkIFQAS3bHF4yMfh/tliIDWXp6YtYQIeLhfxvGRj0R3bBEA1PNoZtzRzWqtsnCQt85pENab7SfZZs32nwZh/SBvnWqtsjDjjm56Ho00JbKzg2v5bD6TSqXOJdJ9iURKWtiFFovFD2OPx9zxcdSB81HQCdF2bIygU4PtD+1GfR0K6EHyAAAAAElFTkSuQmCC"),
    "radio_off_disabled": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABx0lEQVR42q1UO04cQRB91b3sCoSYIUJyQOITEDgxN+ASzjgQEJE55Q6EOPEJGGlB612ZjxDb08P86X4OphdmkWWQvE8qqbv+VV1dQA8khWfUfZ4x3DaG20t6Z9Qkpc+TvhMRIQD8GpsveqC+Ocevzj1/AgCtB7+1lh/u2X/f/Rz/fGvz4gQAkiQZzSb26Gaas7BklZNV6TvKycKSN9Ocs4k9SpJk1LftyiEVydFkbM4LS9q0ciRbko6kD+RItjatXGHJydickxwFWwHZ9eTq8uGktGRVNTUX8OTfzlXV1KUlry4fTkIyXV9n1/O922nBPGtakt73HbxBkPk8a9rbacHZ9XwPABQANI073IrXsbG5BgAiS++xjCCTjc01bMXraBp3CABdfYJ9pRnueBdBRylNQLBPUlSapjGInUUked/PYma6zImdNE1jhRVBRVFkILgL6X6kMvBl/AAI7qIoMkpECOLCOwEA/5Hago73TgDiQkSoAGA41KfWlCie2tdI/240i6cW1pQYDvUp+sP03wO5qi+iwu+liNS1uz94nGfHuXWqzDCoC6i6otQVpS6gygyD3Dr1OM+Oa3d/ICJ1sOVq18gqFtsfYk4ksctbnkUAAAAASUVORK5CYII="),
    "radio_on": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACmElEQVR42q2US0jUcRDHP/Pb/2PX1VRk3TI69DCIjSCjorrVRSLoJFhBQfTQIOgSeBAq8BB0iYoojaBDJHQKIjx1K6jIIFqC7HEIrXVZWtN19//wNx1MsqDbfmEOM8x3YB7fgb+g0tOjiSVPBAb6tXWgX1tF/mQt5qgsZ8ryIiAKcLJ7ansU+ceq1Xh3FMYdAK7nTKVSznPXDe6NjHW8+pcjywPdGz74q9a2XpmfM2cTJkX76pjMKgWg+E2YnnRYsFUaGu31b19+nB/7uDFY4gqoXLiAvHiB2x4WnkTV1r3rcjO250ST7dzsmYa0EYD5itWJd6F9eGfWfM43Gzf14+m0l92/cyfRpUsoSzM5um/yxuFdoV67WAriyOr/EEdWr10sBYd3hXp03+SNpZkJwPHu6a2VGWd8Q87GQyNtCUBmy5ZH9+fIvw4AyG3zOXikkaYWA6CDJ0sLH/PGSTfHXXfH2t8YgDCITiUkSW9fGkB+li1D50qM3p7l0/uIT+8jRm/PMnSuxM+yBZDevjQJSRIG0SkAo6oS1tiTXRPTmfMMwOMHc+THQzIrDX5K8FNCZqUhPx7y+MEcAJ05z2TXxIQ19qiqmMtnZlriSLOZDsVPGlGFty8DGtJCHIPaRYtjaEgLb18GqIKfNJLpUOJIs5fPzCw2XA+YgZvNZceVQnFKCGpWRWDLDp/5iuI4IGbRHAfmK8qWHT4iENSsFqcEx5XCwM3mshER9ZI8K3x1mMiHFuDAoUZyXR7F75agqgRVpfjdkuvyOHCoEYCJfGgLXx28JM9ERA2A57vDC1pj9FYFQFe0GAavttF7uon1m1zWb3LpPd3E4NU2Vvxe/+itCgtaw/Pd4WUCrMtB1kci9RRtfd4I9XpsvwAQU8FpMPiH1gAAAABJRU5ErkJggg=="),
    "radio_on_disabled": (0, "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACuElEQVR42q2UPU9bdxTGf+fcv31NbF+DIqCQly4BKY2Q0krt0GyMfIlu2TpF/RRVpmbK1pUPwMiWRkolGgmliXAk5LxAAUXY1zb42v97TgdCa1XqxjM+Os/zHOm8wBTcXTY3PZnmOp3uXKfTnZvmNjc9cXeZ5mTaREQcYPfFwbdaDT/ESfw+TuIyQKiEg1AJv9k4/rr23fLv/9XINLG1tZd+uZT97K4/1tIaoiUaLoIsglvCqBghYr90DvOfNjZWi0utfG5R2u125bxX38qac+vFJLcvbmZWb6QaggpAjObDQWF/fcg1rWSa90+3Z1rDjZWVlQngCqiI2OBT+jhrzq2TDMer9xa0NTsTQlD93LWEoNqanQmr9xaUZDjOmnPrg0/pYxEx4CLt5csPX1esulNJiStfzSeAxGgcHeT08wKAZpayuJwRggJ4+8+TclIQJjr+5v79m38ogI3sYZrWWLzRAJA4Md6+PuHgfY+zwZizwZiD9z3evj4hTgxAFm80SNMaNrKHAOru4u4PREvqjVQBjg9z+vmINA2oCqpCmgb6+YjjwxyAeiNV0RJ3f+Duou92e7NWspgESBIVd8h7BUmimPk/e2LmJImS9wrcL2qTAFay+G63N6tcEfT2WqurCUdlhLI0F4GslVKWhuq/y6sqlKWRtVJELmrLCJpwdHut1VURcRF55pYwHBQGsLCU0cxqFEXEzDFziiLSzGosLGUADAeFuSWIyDMRcQXQmj4tihFHHwcAHirKnbvzLN9qca1R5VqjyvKtFnfuzhMqF+M/+jigKEZoTZ9e3lgCsPO882T/zZnvt08KM/P/g5n5fvuk2H9z5jvPO08uPRQwd9fG9eJR3j/dpqxX914dW697HmM0AxzwGM163fO49+rYKOvVvH+63bhePHJ3Bezqjvaq3ghX9dj+BtDMygSDopC+AAAAAElFTkSuQmCC"),
    "scale_track": (0, "iVBORw0KGgoAAAANSUhEUgAAACgAAAAGCAYAAACxWNwrAAAAh0lEQVR42s2TMQoCMRQF33soZBES7O08mDfQq6h7As8l2yz24sKSwOr/tjbWyZxgGBi6Ox9Dvnbd+lDKe2NmJElUwN1dkoewmnNebrt9d+Q45PM2htNrKjBz1FH7lQQkIsWA51QuHO/zRxJqlvtX0swgNI4g9SkGSaJ7C/UASUwxCFLP1if5AukPUYDVz1l1AAAAAElFTkSuQmCC"),
    "scale_knob": (3, "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAESklEQVR42sVVf0yUZRz/PM/74+74Id6Bx10ihPFjoc5hEzcCGqIto/7T+sO2ArdWGRu1WjJrsdW6VuaYWyvLzFxuGs1IN6wlBR4YwRBNmZCAXkAcxy85uXvv7n2f9+kP7uhALl2b67u9e7f3ffb5fL/f5/P9fElR+h7cy6C4x/H/EjhdDgEACZ8TDjdWpXx8stoKQAh/I06XgzpdDhILQ4wBTL6sayLFGTU40fpawbJlCc/IRqlQlMhKALTpj3dG1SDr8Pn8x4szaloAMKfLQYozavhiLLL4kiPg3e2DsuPQs/tMcfJLlCJmhopfPd7R0rf7zReO3XS6HHwxyQICp8tBLrZfFw7XnRU/PPLctwajVA5A7/x5kv/6wwQZueEnOgPs6SZesNnCi56wEhDQUFDtPHvq98fGRm56K6rLWDTJ4hbRqqc/o2cu135kMErlAT9TP327X2z53kMJAUSJAgQY6PGRltPj+OWUB1WOHHV5iryx9PG1Xz2aV7u9orqMA2CxWiR8fnp3Qe66tPMA2IE3+uhP37hJss0AzgHO5xIjhIBSYGoshPwSM/YezNNkgyD+OTi5fWfpvoZogmgVEQDiCpu5ghDgwrkpNDd4SLLNAKZx6IyD6wDXAZ1xaCqHJdWA7nPTaP7OQwBwsyVhFwApjLUkgRwXLxUCwPnGcUoIwHlsGeuMQzZRtP04QQEQo0nYsKPyYXM07gKCyle2JIuSaAOA4UEFokzn27JUcM4hSRSeoQCCig5RFszrN2XdH6sCypguRCSp6/yup5Xrc+0DQDjn80N42yTXf9E2EwyokwBgTzdCUzkIIbGRCaBpHMl2A4zxFKrKvV2tfWOxrIL7fUHdOxO4CIBv2pqiM42D0tgEgkAR9DMUbLZwANx3S+lt+Pq3aQA8/Cwg0AFovZeG6gGQgq0WbCy1YGosCFEioJSAEIAQgFICUSKYmQwhe30itjxl0wGQocHxegBaGOv2CgpKstlbLx5rG3ffapQkQXj5/Wwtv9iMKU8Iik+Dps7JU/EzTHlCWJ2XgFf352rxiZLovRm4sGfX0YbMnFQtkj0ACOlJRfMMVvtyzjmnHc19HY9sW1OUZDbaS560suRUGcEAA9M4ZKOAVVkmlO+8jz9fu1q3WI2i4g8Of/LemcqBXvfo2g3pyo1rHi2m2dnSzEb38HTiQ4UPrNq7f8cHK+xJZZF/QYVD1zlM8f8UPj0523Wkrun1k0fbe3LXrZztuzyixKwAAGa9Ad1qT9L7r44GThxqbcrMsfbHxRsMokjjZKMgSzLhapBNTU/Mdl/pdB2s2Hbg3auXhoeyHrT7rvX8FYgGX7KCyN3Y0swy01j8uNtrAhC3Jj/dnF+YaRcEiVzpuu7uahuYBKAA8FntSYpndCYU7UF3IohYhxD2Fjn8plGKUwGEwqph0cq540aLqCpKcqEl1qsePqP/25SLd+EEEQD2X5b+38zk1JYxekuoAAAAAElFTkSuQmCC"),
}
UI_ICONS = {
    "arrow-left-right@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACf0lEQVR42qVUv09TURT+zn2vfS1IUbQkpkyOJCZQMMEOVII6IZOvEeqvKINx0tXllcVN4x9gZBFM+hJJhDiYSCVQBrQMJtaIcVGBCCpqS9tX3rvHoZQfFZMqZ7z3nu9+5zvfOUBFGGCBvUY4nFABoK9lIhwNTkVKp0zV5G4+MsIJdXCyy460Pu/0aU2T+eLXN5/rii2NK36x7F+RlYknAKQbV9g0I84mkAEWgyDZ3zrZqbkankq253Jr70+a6Uix2mqoBALuC86cdglPnAiedScXE1A+smCNpdjBhlmSECRV8u6zZX5xeC40CjCpaR0EkyRhZkBz1fuyhaWCorhuuxUfmB1gF+kZDtyKD+tWbhbAqIEYEcBkIEafQqdqi5Y66nE1dGeLS7c0xTNUgOXTpHDKAFIKAoB1Iblea8Tqj4UvZrorCzDRVmeIe9qe1DRQYExRtC7LyZwdeXX8cbUabRAnNsBiPNWby6sfemxZeEksDQMswuGEqutxxQALLllBbFnlL9bQ9bgCAHrTHW//0akDpbINYWwkRduSDy8eS03rHXFvpXnpXwzb1548V6seemTZmcQqL/aMp3pzZVl2AF0Jva0DvsGyVQIATbW5fJexPKI5NZaZb+2+VOP2Pyg6vya8rJw5nBorDCLGpOusmCY50WDyvlv1XbXsjBRE4s+WA4IEHOkUGBI1roOevL06MZIKdRtgoTab4A2nvWDINRB+SmIByZXDRFKyJJKKgOuaZNsjQM/+e7AvtM/eHeh4x9H2mRvbBVe3d6x52b+r+N8XAsrrwIITyGpD+71Hzn9fm785PBe6Vxp0sqtmUf61Pzh9ORpMXt++cvYUuy2/33bLGdqSTRvnAAAAAElFTkSuQmCC",
    "arrow-left-right@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB9ElEQVR42pVTQWsTQRT+3sxsQhINRVoUPBZPgsZN6yGhFLx4EMGDW+gmV7WoGDx4lCC9ebMgSm9CqdL9BRak0JoapTEqeJKIKAGREsS6Sba7M89DSaIFIf0u82b4vvfN470HDEAMJhwAau9gchwI8kgXs9VFZq13x5s38RESJ6H/lTjwPOq/9d2K2df3r+W+cGFi884wzlQGi8ap1QTH0vNJNXq7HW6vszF3ISlGmplI8h5VgxkEojRFO6tLH877BADFsxun4zT2Loh+RoZ1x5KJw//3E9C0k1l6M/WeHKxIDzPazVZuJa2xB92wtU6CrhgOKSLDIrQY0kgAiKuE9iM/8GpTX/u5ymABALN2pTSX+8au/WphqJp7wdXslrVYmwhdu3IDAMLx5uNj9Yz6fuZEZDU25wBg+W3+YY83dD8LduXR9XyTZ+1K6e+f9pzJmVw7GqNEmzuhtMQhlilfB5GiXa0oJiOGoSdJdeSSH/4oLdfyCw5WJAGAO/kyo5Csa9Pl/eUMrqYFiHRcjai2btnPtvJ1BQAi8D9pS7mATAEhANGXMbMQgn4Zw24qNnrRD7dfaLQ/l8FiqFkuZCuX43LkaWi6G10VXPCquQ7ApHqE6ek1tV+UaHZk53hC4zfORRw876qG41VnOmWUxT2QwcEx2Lw/CP3WYapJpNwAAAAASUVORK5CYII=",
    "brush@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACuklEQVR42p2RTUhUURiG33POnfszmZMFQmabhhESBG0GsjK1SMO0FsHNmVEqihKClkG7MGjTqmjVD23CHxRc1GARmaUVloyKRYtKydJ+CXOa5s6dueecFjVlgqa9q3MW5znP973AEnIKp2jmfMA/uCVU8mBT5k4WD5G0GUTU+S+6V2DDVU3JqpeQSDrTp311m5vJUiBmaYehO+u6DVdOZdx+f5QSl09h+omUiHnpUiCG443oiqcylpoKtw6VXebSmgIAyhmlS4FoSvb2b8nJcHu0vC1Y3BfKUtecS/PvXd5s+zVZzE5WUn+3xjwV35KT4faRbW3B4t7Qcj2/1eaxe7qQtZeifov9yyQbhdcV4q74bn9qbB+p/ANxYnctZaz22mCVBWDuZJJUVPQqAFC3+qK7IfC451jZBxksuX8OAIL+vtCR0pdyfyDaY5Z2GJlP/6o/YwEAZukjw+D6bQJS5ghrlBClgAvrsqbkHBcydd9i4zWdA/us2W9IxgQgMlwUyaHu/ALCU6dd1F1upaf3tQ1vvdHgfzjqMbxFMetN31cM10SiTYnZEACgptnBACKDJff2KFreKxWuAbeaW53i8QlHY3dCJf27VebxxZPv+uN0Zm8k2pQwzQ42G/LLSJLgxoFcVahjQjgvJNInIcl6xrSzXKQmFKr7ADxIsFfVc8eZHQoQqaT5DlVZvswR9sGWaNntlqEt5zlPnsnS8nxcpHq+iMGdC0GQqU0ARSknBuZSX2da4FKMp3kCBOlDkWhTwoRk80EAgNQHnqx1SbwQMnmldaj8uFn4TFXZ51yme2463MpZlTvjXXmrJt0MIgHI+UCUwXZJOIxRoyHsf/TRpc98VnTPWwLik4I0XLi1y35udpKFIL/rbww83EyJcZgLWyMgkhJlIm5PX+16WjUuIQn5afP/yexrMfkBw49OJ81+zG8AAAAASUVORK5CYII=",
    "brush@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACCklEQVR42q3RT2iSYQDH8d/zvK/6ltao2KoF/bkUBWu5J9hIl7nTILoEkqlzh1FhoFSDruKtSyMTugQenBJsdO6W2nQO1HUIYh0KD5vO1S5Nnb76vk+H0mIZ26Hf7Xngc3i+D7CH2WxzAgC4Li0MTg2veOzGVD8AkN2g3xIXA0lry8HSPq1gCKpqU1F4/SNXyQ26V9gjnQkqaj2y2fjUS4luAJTcpLtBJ0t5e6RTwUp9LTqbG548rDs7I1AtqELf/hMCgHNo4YHHVORulo0AwATLzt43FbndmPJ2c6QD2aJ3amSFT1zOvQAAN8tGPKZV7mBpXztiJ5jFEhf7+q7x+Xmi3Da+e3RAOvFUVqqoNTbGNKLBflA6efd7/YsvljeHLJa4mExaW3/VdrJF735N7/Pt5mZUhdqvFQxjAtWiLn99GFsefdZuAQAiwInfkhA+V6Q7APXqdUfP1+Tyh2h+xO1iSxGtoEdVLvtiy6OhP+Gvf+bExZYe67XHnmzJpTAhpAZOJykhBUk8NFBplKZfvb86sxN25mDpnJNlkr/PmZf3rhS4k/2s2o64c/TWYOa0QHTngFa4cwkuVRprhVjeHLLZ5oRA0qp0w6KgUa/v0xwxVOR1s2MofZEQekGvOz6+1Sh6/OA0sZEgAHhXDGjeVOXyawo6zgkoIcK3SqM03dxeDQcAjm7v/B/7AfyX3kY6T0EhAAAAAElFTkSuQmCC",
    "captions@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAChElEQVR42q1Uv09TURT+zn2vry1QtBZ/xsQBYuJACC1xaDF1MTDUOAFJi2Bc/RtI0+hgotGoMRBjTBzaCrhKHEwaU6gk0DYuxsG4aOIgVJpC+x7vvXscKMUCUYl+27n33O/kO/d8h9AMwl+DARDvOR4amlFwQMTBAv+Kwa45Z5OUOFgkQDLqX7jlEK5rljQUJvy2EgGkkMtlyuqDdKF/Ig4WBAAxf27YpR2ZrpmlVwSsM5hoP/0A6ncSoFaXw3tZN1ejyXworQIAE3oNqyxT+WDkIPJG+xbXAEQApOsSeBOQItqf9e5ODocz6v6NjguWtr4d15OICAK+tuPV0d5cFylahCDYRi2XfBtaigVy46pwum1pVIXi9m2aG9lEsX85hgF1awwaRABB4NHrs0YskLvYqvnuExSUja93w+FMkSu4rantJ0y7CrfDB8uu3gGwjF8+pUEkYeN6cN7zLBd8eqUn87LDfVSk88ESAIz3FM+Zpu5hs1RZWf8mZj8MlurPrD1EBILtMM2xwHynIM/AxuaPldHAkgewtOf53slYd6ZN0dpHWhyCo/5sPlW4kGdmQdRExAwQqse+m47105cOOX2PyVKgkAbdKiHanX3BmhZpcXZMEgmUa9V7cXDxE70Tu3pEGoPl7OywDWBqKLCcBgCv9xQ63yQrCSQkgKnY+bmUapwUqfehNQC4Sou6ZOaGSWP+3LBb803XzFIKzJ+ZyCDYDAmAhMKARgCDSGdIEiCNmc44He1jhlkeSRaCM9sW4ah/4aaquG5IaR7eoqcdl3N9yEk0YkFaxZL6w1QhNBEHU9PaiIdZ/WJ+dAOrf5hpH1RjQ3+S7zP/+xrZtciYDka1Y+yfN74MJnMTO5sAAAAASUVORK5CYII=",
    "captions@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB6UlEQVR42qVTv2tTURg959730qRNrGDtD3AXl4KJYE2Q6ujiVimNQ0c3/wCVQoSCs1sXlyR0cHFVMCkSQ8VkcO1eKDZgksa+mPfucehLi6Jdesb73XPPOd/9PiLG8nLNu36UIc7BQrqvOoCdnfshLgoCIkCtZZvrnvFvRC4QwD8ciCAlZ20qE7nRt0praQsQCABrucbzhE2/HIWDAxAW0H90EPre1PwoHDyrtPObXlxY+eUGb6vt/MrYyd9UQSSoYrb5StATAJvmhMsAcuOH9O984/MQAHwAOCE4+QR7qzebiwmbekdKLhw8isjbSW/6aRB1+wmTvhKE3YegOQJozsiUICWA6JB07yUwMPt7SV6bElybsF1BAswhQR+SAMDEppxgvKv92Y7otkP1Xl/yF++MIrf35sviqm9TZbjj7R/9XsfB+Yo7GueUIdnvTB/eSnsLH4cy8M0kjGGpmPtUS9hMDTaDmcuuIKErwp4qC/All6m28o2f2p+LPDM3NMFstVXYqLTu1o/D7zM9dzBf/lr4LIc5CC7+PKCYa7ywJlmK3HAXoFOcicQEBAMiiDs+4dnJ7CgcbFTa+dLphD3O7q7TmAdS6AsiJIpjBVKgLP1ILvxQbi9tnfAuNttnW3UPQP2cy+P6eKt+A4F85GQdpUxkAAAAAElFTkSuQmCC",
    "chevron-down@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABaklEQVR42mNgGEngPyMV1EAU1DP8Z8KlAkkOv2HGxjNZcRkGEzM2TmPFYUs9EwMDA0Ok8VGNRLNLzyIND0UwMDAwpBmfgWuwt9/PwsDAwBButMc2yezqk2jDo84MDAwMoaGrmBkYGBhYGBgYGBoZGv7XMzAw3WNief3n/8/7POySyyOMD/2ZddZkTb39fpYDDAwMBw86/ok03m/DziS878//X/dZ/jHd/c/wn5FxdcN/rOFjLz+fI87k7N5Ui9v/Iwz2h8BkI4332ySYXvodZ3r+aqDeWjFkn2AEWD1DPVMjQ+O/UItVnJx/lLewswo4ffn13Jvx//+X7CyCJ/7+/33r889Xjusvub8KDV3FvHp12F8GXCFfz/CfqZGBEWLYX+VtTIwsDn///frPyMh88/33xw6br/i/RDeEgVAU+xjP5Io1Prk3xuTURV+dPeLIgUt8skRJdJCwQA4TClI5MSkeP2AkmIqHNwAAG7GJFeeFbuEAAAAASUVORK5CYII=",
    "chevron-down@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABAklEQVR42mNgGCTgPyMDAwMjDklGqDxBwEiyvYkGh0TRXMHAwPCf8T+UHYoiz8DABLPFQ2Ub+08m5huxJqdWMTAwMNQzNDDWM/xnCg1lYGJkYPwfZXJ8Biszy61QrStsKK6rZ/jPxMDwnzHa+HhRhtWj/zHGJxcjxBkYok1OdGRaP/kfZXy0i4HhPyNMHA5gAlHGh/IyrB7/jzE6vsxDZRt7tNHRzkzrp/+jTY53IqvDAPX2+1kYGBgYIowOF6Ra3P4fbXz8ZorFrf/Rxse7GRgYGEJD/zPjDUyYAdGGR3KTzK9/iDI+1o7QSERUwZwWo7eTm4j4x21APUM9EyUpbhACAPCIWS5xXFajAAAAAElFTkSuQmCC",
    "chevron-down-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAApUlEQVR42uXSOwrCQBAA0JgYUFtBW0t7W63srC28rFh5AAsRrMVLCD4LE1hDfpJKnGZZmHm7M0wU/U+g1zknT0BckxO3xdIqLEDSplfmuGNXLEA/O1e4YZ3dk4+WEGOMo3dscyBAlnjgilleVzWfAfYhVkDOmNTOMmhxGGAbLEqQpGnYIXbIsCcumLZCSrBR9rPT10jZ0gVw3HnL22x8I9YZ+e14ARTH9VfJaiBIAAAAAElFTkSuQmCC",
    "chevron-down-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAkklEQVR42s2SSw6CMBCG+zC6cM0xJcRLIHIB9iQsvBGHMHqDzwU/SakNoayYpJnMzDevtsYcQgAL2NzYH7incxEXCDuG8Ri6AG/gJdvpeNkd8AHOi+kEWeDOJP3sl37I/xTn4glmsBQ4aJpGdhNyqZ1P0pUSRulWfr96mUGBG/AF6k2JiRWuWW+cKOB2/zhzSPkBXZjK4zWEMr0AAAAASUVORK5CYII=",
    "chevron-down-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABbUlEQVR42mNgGEngPyM11DAyMDAw1NfXM+FSgCSH37C0tDRWXIbBxNKMIWpw2tLTulRjUve6Zx318yMghs5kRajZz8LAwMDQWjvLdkrfhic9jYudGRgYGFatWsXMwMDAwMLAwMDQ0NDwn4GBgYmd8c/r379/3BcUEl3eWj/3T3Vj8hqYAY2Njn+a6+fa8PEI7Pv769d9ZjaWu////2eE6kX48////4yMjIz/6+PrOQQ0NbdycfI6vXn3IrS6MXkNAwMDQ3P9XBsBHoH9f//9u/X+1QPHxt7SV/X19UyNjY3/MAIMJlFY2MspJy69hZuLz+n9h9fe/xj+v+Tl4jvx5+/fW++/vXZsbMx6FRq6inn16rC/DLhCHtkweXGZbYxMzA5//vz+z8TEdPPVx+cO7e0FL1etWsUcFoYwhIFQFKel1XP1tC7d29u2/GJl5Wxx5MAlPln+R050EIPxpS/8hiGlYFSDyQOMBFPx8AYAmhmeLS9z/o0AAAAASUVORK5CYII=",
    "chevron-down-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABNklEQVR42mNgGBTg////jAwMDIw4pBmh8gQBI8k2t7auEkVzBcP////hNra2zhVFVs8Ms2XixIns/36w3vd0CTXZtW/tagYGBiYHBwfG169fM+no6Pzrbloyg5GBeWaaXWT/6oOr/zIwMDAyMTAw/K+vr2fKy8v7xcjA2CooKBra07Z8cWNj4z8GBgaGsLCwv12tSztERCXSGRn+zw5rDP1dX1/PxMDA8J+ZgYGB4eDBg/8ZGBiYaurzj1maub4XERYvt7F0V3/xhmFzsE9km7ioVPmbt8+7ymrjyuvrGZhgBqOA+vr9LAwMDAwdjQsLZkzc8r+7ZcnNGRO2/O9uWdrNwMDAsGrVKma8gQkzoKt5Ye7Uvo0fuluWtBOlEWFAPRMDAwNDd/cibiLiH7cBMJrcFDcIAQCNrHri5/r9bwAAAABJRU5ErkJggg==",
    "chevron-down-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABMElEQVR42uXTz0sCQRQH8O+bWWsTgiLdzRC8rmldvIWxZtClog6NJf0xS39OGAj9ukV46xpIYAcjiGDtKBKbuztd3Nii1QhvvuPMlw/De2+AiSoaR2YQsFh05OtuOFZAIRaNBWdBJiKQX6gaK3r11UgcHH1HAROmAgDZxN76qn78ktUONwFAQPDw8wiwyFhqzyuuc86Zutb3uuLhrX4WAA003OXkfpGz+K2E/9SH3GrZp88Dw6cf/ZGZjKnOvi9ecTZdDjAACBBI/7FHzkbbrncAMAD+Lw2zGHDip9NiZu6DXSpMLTtud1uC2zE+dQfphRDBgZo3ZIxhjF8TsZKUniRQS6JXanYubAHBayEEo0acSu3Ec1rlJpes3Oe1XT3c3P8uJhu9X3/HaBxfhjC59QkjmWW7mJ8iqAAAAABJRU5ErkJggg==",
    "chevron-down-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA2klEQVR42mNgGCyAEYpJlcNQSBowkAgVxWIA3EY0eQYmmIQKgwf7778MN7TFwldBpOoZGRjqmUIZQpkYGBj+a4uGzvj1j+GWFkMoG5rh9UwMDAyMWmKhRYaSif+1RcMWI4kzaIuFdRhKJv3XEgvtgmiCiCMBiICmSHAe1IBlKioe7FpiwZ1GUkn/tcRCO5HVYQB7BnsWiE0hBQYScf+1xUJv6kvE/dcRC+tmYGBgCGUIZcYbmDADtERDcvXEYz5oi4W2E6UR3Qt64jHcpMYxA6rfcPiRJomFLgAAr/IvAjsvjsIAAAAASUVORK5CYII=",
    "chevron-up@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABWUlEQVR42mNgGAWUAsZ6hv9M9Qz1TAwM/xnJNAObRtyGseAy5D8DAwMjAwNDtMnxhYwMTCIf/l8I3XKW8Vs9w3+mRgbGf+g6mLAZUs/AwMjIwPg/xuTMUk4WoTh2Zj4vQUbjraEWqzgbGRj/1TP8ZyJgEMSQRgbGfzGmpxfzsktEffv9NuXrn1c+HKwCDpx/lLfgMwzD/7GmpxdnWD36H2V8KA8mFmF8KCTV4vb/OJOze0NlejnR9TAiu+Sa1lUuds6vc3jYJcM//Xycv+ys9aR6+/0sz7/wMs46a/I7wvhQCC+r5Oqffz8eZP7NFCp3YePbRoaG/wwMjP8ZGBgYGOrrIc6MMj6mm2h2+XeU4ZFCBgYGhnr7/fDISDM+w8rAwMAQYXAoJMn82q8ok+OeDAwMDKGhq5ixejBCZ484AwMDA7YwgInB1BAEkASISw5nIBNvCClqhhEAAAdXeFtHv8PkAAAAAElFTkSuQmCC",
    "chevron-up@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABJklEQVR42mNgGKTgPyNZ2uoZ/jMh0yRrNDaeyUqSAWnGZ1gZGBgYogwOhyaZX38caXw4h4GBgcHefj8LURojjA6EJZld+R9jcup9ssWN/9FGxwoYGBgY6tEMYEZ2at9z6b8RxodCeNmlV/38+/HAnx+fHf4z/pHg5ZAtUxMLfD/luMPxeob/TAcZGv8zMDAwMEI01jMxMDAw3DZyjeNiE5//68+nfe/+P/XdctbvGwPDf8YY41OLeNglYz7/fFq89KxFfz0DA2MjA+M/RpgBuSq32N7xv3nCzMR29TvzPc/VJ8K+29vvZ3E4eOBfI0Pjv1iTU6v+/vvt/O7TR6kdd7x+wvTBQYzeUbFQhlBmZNcgx3WiwSFRchIHI5GacKYqRrJTHNUBAA27cgYt/uI8AAAAAElFTkSuQmCC",
    "chevron-up-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABW0lEQVR42mNgGAWUAsb6+nqm+vp6pv8M/xnJNANT4///uA1jwWoEVAMjIwNDd/OShYxMTCK3Ht4MZWRk/FZfX8/U2Nj4D10PEzZDGhoaGBkZGf/3tS1bysPLH8fFye2loaC5tbCwl7OxsfFffX09E16DYIY0Njb+62lbvlhQUCzqy+f3KZ8+v/Ph4eF1kBOX3oLLMEY09n8GBgaGnrbli4UERGJevX6eX1EfN4mBgYGho35uiKCQxOpv3z/ve/TypE9/f//3////MzIyMv5HNoixvr6eUZRBm+sn2585goIi4a9ev8ivqI+bVF+/n0VK6hZjenr679b6uSEiQhKrv337fJDpH1Pou19X3zY0NPyHGcYAc2ZH0xLdyd1rf3c2LCqEiO+HR0Za2kxWmMum9m781d2yxJOBgYFh1apVzFijsbVytjiy4cgAJgZTQxBgM4QYOZIVEm3Y8AAAdoyUbC5PKFUAAAAASUVORK5CYII=",
    "chevron-up-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABGklEQVR42mNgGKyAkSxd9fX1TBD6PxNZGtPSZrIi8wkCmIb2xvmhU/s2Pu5oXJQDMWA/C16NM2dCNHbULwib0rP+f2/bsvfT+zf972hcWIDNAGZkp5aUlPxtrZ8bIiwkvurb988Hfn755vD3/z8JMRHJMksz1/e1DQHH6+vrmQ4ePPgfrhnmJ2/n5HghQdGl375/2ffp52efupa0D7v3r9tga+2lLCIsXm5t4fa5ui7vOAMDA9PBgwf/M8KiY+LEbWw/Pr15wsrCdvXxq6ee/f3F3+vr61kYGBj+NTY2/uttXbbqz78/zhx876Xy8/N/YkRjd/0isdDQUBTX/P//H66otXWVKMEQ/8/wn5HkxAK1BZdCRmRXDCwAAKZ0fvjgqAujAAAAAElFTkSuQmCC",
    "chevron-up-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABIklEQVR42mNgGAWEACNh+Xqomsb/DAwM/6llCSM5hjAyMDAwaImFLtQWC90qKenDBZGqZ8KmgQmPd/7riIctZWXiiGNhYvcS/suzVUYmlJOBofEfNsOYsBvS+E9bNHwxGxNP1J//P1J+/vnpw8LM7iDwi2kLPsMw/K8tGr7YUDLxv6ZIcB5MTEs0MERfIu6/jlj4XhkZC050PYzILtESvcbFxMg4h5WZO/zH70/519+snWTPYM/yheEL41mGs7+1RANDWJl5V//79/MgM9O/0AsvtN6ixGY91JnqYqG6uuJRv7XEggsZGBgY7BnsWWA2GjMYs8Jcpice/UtTJNyTgYGBIZQhlBmr/3TEfMVxxw5EDKGGIMAdkPjlSFZItGHDAQAA7cJNOVRLsmoAAAAASUVORK5CYII=",
    "chevron-up-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABEElEQVR42mNgGKyAkUx99UyoNIkajRmMWUkyAKZBSzQkVF885rGWaGgOAwMDgz2DPQu6WmZUjWmsZxm2/NYUCQ5jZeFY9ff/73+szBxBIlxqH09/3X7MnsGe5SHDw39YNNczPWfo+6slGhjCzsqz6u+/XwcYGb47/P3HKMHOwlMmyKH8/sy37cchXjj4H0kzxE9aomLxrMzcS//++7mPnfmbz8WXWz68/nZ1gzCnmjI7K2+5EJfq59dfp8ENYIRFhwqDBxu7KM8TBkbmqx/Z/nk+ebL6uz2DPctBhoP/GBgY/mmLha9i+P/P+efrz1J3GHb8xIhGPfFAMSSvMKHHtYFEqCg5iYORWE2MZMjRGQAAmPpWL7e+ZkAAAAAASUVORK5CYII=",
    "copy@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACmUlEQVR42pVUPWhTURT+zn335a/V2CoOIgixKHQxJkhtLcagg1IRHFIkEYqTY0FwcYkPF3FxL4LgULBZKjoK8Wnaipr+CBV1iAiWigXbam2al3fvcUjaJk1a9IMLl8M59/x837mEKq4eH+8gaQgHJXhQD7dscosZpAXn048nM5eXACaAuNaHAFAymrvvNYKDmstV01YwiAxorb6uoXhx5F3PbDoNsizS6x5yIPI6ImTroOOuDDHzBIgNoVnXPySgUIZpBIZMpgcAnYTFdRmlY6h9PmZmxtDwZHceOyAZzR0hiMOxWFbOr+Qp1poVZ+wX2oKlKXVi7KyHdj93+Ne5bwHH9vuLRlfRryw77uKfwSSZiUAAtFC2HXdjsSwsO+6mIhODhmF2K+UwaGNwohqoGeSXwlculVZuPX5Pn+XWt2077qYi4zd9ZvBe0V2eB8FoUgIBcMF6r+nxhq+FJ7tl00qJk0V3aWY43xPecWbHXp7y+/fnSvzzgqglePPOZZAuAUAaLBrFUGHMhDlXVn9YA4H1nhmCVCIxUm2DCEyiRlRUe/qREQDIIZgEQQSwFEwkSJJWjjeT6VcNySvBdSruRIIBsEG8YRemKk+X1O+5gGwfTUVydyvpiZorfHuIh9OnFxzX6WPwK0Ac2izk/0DNFjAZzb0BSJRDPV1thbxYDBXqVqatEBKLoYL2fDnQIanlo+LV6xIgToMF0sAHC5QBKQAS0GuZDCkACo2Lo5AHBjqzy2iBYggpAcACaVhAAmxUZ/RdkK/3SnSsT4BXKzaDN2WmBZPWLpvnJZEhXDEl69m4zQCTxvgNA/w0IPc8460fQR2hAiW1dGd4qneCtpE/Xzo6uqs9eDCstPYqNO6vAQkinn/0tmsWAP4CFD0a/aNARYMAAAAASUVORK5CYII=",
    "copy@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACJUlEQVR42o2SvWtUURDFf3Pv3X2JRrDwgzSSOo0mKkaSGETQYCFYPMHVLr3+CZo/wMrWpNEVzDYWYqGFhHwasimEqBiIqJ1NMB8m2bfvHot11xAUPDBMMTPMOZxjALdPTZ0ktA/EuMteGE7BtVldWyvl6sArkIGpNS+dnT7lYmEKdFDETMJhEjLDJMNi0R9KanF9pLzYP56m8pWK5QCOaBdEPGhkXXh3NLPiEWrxOFnHMe9Cp2p5Zy3feifZyNDQm8Dysk/TCQ8QELksZq6j/v3x5OAOf0HaN9u3uxbDZPViHYD3ALLQpJ9s1hPQbpriktW5O2bJYK7dCHLU2Cl2mEq9s4l3yaE87jx8umQvmsds1tocmIqrc3eDa3+Qxe2qCU9DuBOGQUR1Z+ae3+x5Oxj2U5TipSz/uVBe6j/HP1DqnflmxBvBQEL4rOgAw2wbUbiHHOCA2JBZsW5SwX1WIGAuhAgFh49fP778AQjwAKNYvIcYxWLL9sacEpcBFJzpQ9F3JF1nrs2fsKtX8jzfluH4D7hytf91La6PYH5tx+cyk/Z8avY9JQMwkwKYyouMA+MAt07PtBmqA7oPjP6mSqsbMJMTzTuANJUfHv6U0AhuQC5prP3JcSsw3RNFzB2W00YAqFQsT9OJRmyixpwPz26dmf9iUiEiUJOqRWeFA1Jcy8ge2T6XDUw3e6bPF0L79TxmiZRb0zFvBZm5jei2xp4sDH3+BaQ3+T1FjV2RAAAAAElFTkSuQmCC",
    "copy-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACrElEQVR42p1UQUhUURQ99/3/5482Y422sU1ELsI2EoRZbaKgorZ+SEKCFi2HpElQ8vudLBuEcCuCpEkwboKiNm2iQAiCCiZE3LSwgQK1nD8zf+a/d1vMjP7RkurA48F999173rn3XUIV9+yZtpBpiJJXQsjEFjxAajrHYvtoZXnpmzNxcx0AAeCAFwgApZxHDxsao3Ff+iCqmQNghtA0SOl/8aV3OTHYm7FtJschVXPRx0fmjoX3NMaL+fykYrlABE0CKhhHAyCLCiEzPMmKpgCcAIbrstH43bnzZrjhpZf/efzW0LX32AVjIzP3BeiwK5d7stkD1Nr6lQEox3EUjY8+OWua5ivPdc9tqOXXzc2d2upqg3ScMz7+Gky6UooAgAWkYzu+bdtwHMd/MDIbNwyjq+yXmbgmGouqzgokGgzDKBcKxYEBh5b07bGrQRKRSDTlurksAI0pWBuAGARWPjO3mCG9Y2I03aX/gWuP6+Y+3r5ztWO3B40606diTS1vcxvrF0XNqJTcyktcBrMHALZtix2KcIWjUFjxvCKTEI2iesBC6LK7O61VtSMAItBQFFyWZQkAJLSQQURUuS+INE0nqUrm/LwltycP7Jurvb2dAbCUcrO7hYHCh3whtxKJxJ6mkrNjFUJM+EeI+OD1736heAmK3zBwEP8JYmYioroPmEo+fsfM4tARs3NtbU3EYrG6L1OzLX7y2pqi4UW/XLqhExFXKjOMo5/nyZq3JMA6wEXLsiQA+RsCslLR6R8AJEPpdVqk02nNsiyZSs6+0DXjdKlcvkKQeQAQQguwZuGDlAZxwTRD/Z5fOlnXkJlMhgGQEKIPxM8ikchzVrxjqtRGCxHBdXPJfrt34XcuBIATialo6/69HX6pZAI+gGBOH7puAmWV7RvqyQDAL770OSWXKRICAAAAAElFTkSuQmCC",
    "copy-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACK0lEQVR42n2SO2hUQRSG/zMzd29ugiD4CGli0Mq10ELQgIpir9hs5dqkVyzcVJvLXSWyKwrBWiEbLbxBSGGjljZiKSQRBB9oFQvRmH3dmfkt1hvzMP4wDJyZOeef8x0BgNu15tEwCE9Z2wWgkEuE1Log1vbeV6rlFwAEANfPG7ceHVMir0gOkcwAKkAIUEihCHwUDYWt1trE5FT5YZqmulQqOQBQApwhOeQyN7Zmsc8HA3udUcPO6P3O+BFn1Ein3XorwEQcx2ZxETpNUw0AhoQDfNbG6EqSnOvgH7p7PT1pzU+T3Ens3yjF5L+Lom8hye78/Lz6/K53VRtzOrOZB6ic6nTAkPXaXFgoFHb1etn9ySl5tt4dMlMiwk/L3WuFMLxnXTYK8JCIjIFSFOAIBAets8NKcaGRPB43203yfLfbeVOZKp/ADmrUml8oLJl+TwHSKgBCkbYIgjiO1R9uHgCWlpakWCwSfVYGoDHwKhBF/7z74QcAikADQJIkPo5jJEnic6w543qtCUCoRPzywMBgeDE6/Hp6enYPRNqbJuU/UpXq5Zet1uqEFv097EYUkhsq5fuGRenzEcq2ZtycewrgQKVaPk5SRIRb79ST5leILCgASNNUz8zMhP3MYgAJ8+xbH8ZxWhCldkPJ6nrlfGbrtbkLQRA8cc6tkAxADwICEQjgtTaDpP/lfXZ2k+3cZiOZHTdhdMlmWUg6yYlpramUXnW29+BG9crH3++HAvACfJtpAAAAAElFTkSuQmCC",
    "copy-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACUUlEQVR42pWTPWtUURCGn5l7snH9ACVu1ErQ4MeuxWKjorCWimIhXgv/gGUqCy0Mi43Y2Ad/gLgWEf0DFwVBCEYlMVgIFhI1gkbEmNxzzljs3sTsh+ALpzhzhnfOvO+M0EG1ko45U12lF0JuQ2yRpfzHlw9Lj74DAtjGHJBqJb3rtDRuhE6IvnRm8YOXeH7+84PZCSakSTMWr+7QnvSoi6XxEFcnDZ6LkZhY3EiiQEQlmUyi3QOON7sqOvOyE8XUMflmoTXNP3B4Z3pAlf0NGu4nT2QrDc04HaEZnYp4QHyI2xs0XJlyssxyyMh8N9Hbr63rvfQZgDgwAVAhZGS+QYOMzNdG03EhOREJVggn7R4xiGKURZPch9835r9OvXO9/JmvVdJrTofv+LiygJEUb7bukCB4jBGnpXp9d3rCDTDoSh5XXs19adX/pVmtcvGkS7Y8y8PyWV0P25oLZuRirLRvE9pL0c71OvQxWm5RbHORZCoESIs2xNp6yN/ztn4uKyAa8iEQEcScYSKIxGjD0ArdpftNMVQNMJFkLa6blBlvqx9VNk/VRi/d7tSXgQM+ADrzqbWI5ecMnhqyd4M9/wHp9/VqJX0BaG2RY+/Zofv4tmFlitjrEcZKzs0H81ddm6RwZk6gFQScwe9WW7PQZ2/CNFDVdAkIYuY6c1Rsccc14ZOipw7tSs8lXn7hfMfIwgFV8TEicgZIRORlt6QK2JGRiwdxQ49V3Fh/zWxtaUJYuTW7+PCmDNDNDo5c2FaSch21Yd8nyQFedOHt5/uzAH8ABiDqfUGiLNMAAAAASUVORK5CYII=",
    "copy-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB5ElEQVR42oWSP2sUURTFf/e+NztoUATdiRFBSxkDWghGMEawSSUKvnyB9PoRgh/Aylr7sARSWJkq2IhgI+xGtJCoTTbBoKvsTmbnXYv9w5JEPPC4xf13zrlPAK7Wl64peruiYhKCmKpKjOXn5u7aa0AAG+fzerguIm+AKbDSQMFsVChIdFJLy+pgeWuv8TIQXINGBaBm3EFsykq7XPRr9V7JOVfUpl1Ry2L3xIwWyUwVyw+iLC+w4JvgIDgAL0KFxfLP6b329vZmj2MwdzHM7XcLv8lmf1KVBzBE3K8sBQoImmc8FtF5sxgxtFNYz2tqefYoVfGnDHve3Fl95UdjpmKigOUZT5z4Z5X13ws4BBv6gCARM4W4PpuFeX+UpN2L1n/Xajdu8g/kWfhmxpIXxAyjjE6HDneBBFYUUCAOWloCuQ1O2PII3mOWiEic7nz9+REMwyEATyOsDOPw7OMbBwBTJG45raU/svNvr1x4cFagO9D4f2hzd22j7PeWEbevmhiITWwaxcMPEJNjzFgTuNRsN26AycSwyZrvgqzrQEFwiyymgIjhgXQ0/UgjoSbGGbDOePPoz+ZZuK/oKljbsOSQhAh6Euw3JncP0xbAZrNwC9xDI6YD6uO0iWjngOrFp53Gl7/4s8acXXcvnQAAAABJRU5ErkJggg==",
    "cpu@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADn0lEQVR42o2UTWhcVRTH/+fe+9582HFIQ2hrIxKs1lQkZm61+WhJBvEDxIWLETqJIC5Gl7W4MosY3DcV3SQroaY1ia5ULK7mlc4kmryUgB8gNQ1WG1LJkI+mk3nvvntcTCapC8Gzu3DPn9/93/85hL1i6usryqMVlTKubWN/dfFuXwsdXo+3CoJzJ71zCwCO3ou1i5BXbzcFFc/rjwBiABANmeG+ovS8rKEYXk+KdDl1/JGk52WNVDxKwn7teVnjeVlDzMXI4SHPyxqtfdXoJ4CpoH017p8MBzLlc0I4bykR76hFW98DvA7GC0KoZstmikAEUE6Ss2nZfPi53zVa0PPOuK8NAUwAcV5ffy+hDl7YMevLzDYgEk3MLIloC4Bh5iYATIQKIDiu0k9WTeX8Zf/0KABQQc+n73FtUArnA2vN6sRCTwb/owZ1+RqR87RF9JEM7bTaRvC4FM6nSiZQs1t/AcBZXXrKofj7FkGSmWnXBSnIVYiCoUs3un9l5r9dlTjIbEdravOWmvB7Fwaen3k0NNtX688EEeiiqw68fD9cWyFAAQCDjRLukYBrzQD6mWACs10JjdWTi6eXFcCY+LHrz7y+vgUiFwAT49B2uDZ7xe/p3v8U8IAuTQB4FQAT2FoOzOTimWUAUPnO0mMkxCdSuO2R3blZTxRMg2QYLIp9ReF5WQOQBBDWCclRMpke1LNXLWNISKViAJ4QkA8B4Ho2GQRQLjclV7QvE4mqzOWmJIPoQcMFKQeg4wSTVJfmTv0GoH1Al+cYDV/hWGB7evqNCEDUaDyryzXB7NbvcBBGW5UJv7cN2MXP5aYklupG7yqtxmXqpbwuze6bDeOIxMnQVn+o6wgFwBb0mDPuvxOKvC4/4y61riqR7ADDDmNYWBGdNzb4UkCqhraAUsz2GyuiAoMJYOmIZMt9dK4NPld+Rdka3RUxnmTwm0REIxixmBv5GUDuv8L4BYBBzMQAbBDxVzKk3/fMq49I84WdcP0GyK4D9DCDiRhVAIYJKWIiBjaIKBZT6e5quHbuysKZjwFA1of2Neczv6t04tDAhhBOr6sO6MjWNgkIGNShZPwYs7kDIquk20MQKWOrQ5cXei8W9LzjrxzZxy3oMWeX7N23T/3EuWevtdTPM9/lM6Xbe+SZcjWvy2MAoPW888Aa+fdia60mDsNyJmjr+vbENHips5SxhMSxhd7SL7lpit1sfZGUWfojGS0Vvf6IdhfbPzB7qBOuLWc9AAAAAElFTkSuQmCC",
    "cpu@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAC0klEQVR42nXTzWtcZRgF8PM878e9c5NpHNOW4kYRV0NROsNIEwRXIoir4hWNKd2olUph6qjV1V2KWLBdCH7gLqnSZOGmiCJ0k5GxJhEhBEpBFAQtpFonma/7fjwuGkEQz59wzvkBAAChAgU/d2ztvsVm7xwAnHr8Wnqytd7J61sWABab35871fruSIGCASEAoAIFb9dzvbJ9tFxs9F5O7D0fTdyfxwPC7JQ5fHXodp7VkW5oO/PjxN15c3lz/r28vmXr2yuesJ+FZvd9w1lbEKHIQhAgImBSEERE8VCcYOJ3L13emG8DAC0c696vWT9BbD8JcXI+InYh4zFH4z1p0eKpRLBasWUkDwPyAbE+Hfzkaw3GM8bMXHBhIGXAV2LUrYwOvhXCoMoQEhiqqCr54N+O8a9fFU/HRFU/HkXXIUBosdl7VKvpXoj9RhB6MTMHz4zKnR8EMAQ4o6ceKcNwlV08q9PpW84PTixvzn2h8+bGAUSuiQoQHyMUT43czvry5nzrnz6eb3Q/J8IDypcsEgDmWt785oA2GJ9Ok8PvujCAJy0KEgmU7E9kAZSEfoWASWnTqhaHir73U5RxVpPSH47cHz8Zla1YAEGiiuDxyvbREkAJAAuNtaEIHRFPfTYWo/LOq9C8pB+6/uXezeaTNwQCUVFBaGDVdOuFxrerArEEKrXKnnJheFVzFBAhUvj5s+vHd2mh2e2kunbBx1EMcdIKMfk90fqdIOMHRUIk0qxhfxmrUYeDn2KxNxMzwyN3+3VtyVzxYc8rTi96Gc8pcdeG+O18RodQeqpmSdrvx9uUBHuIop5TOmHndl8LZFb/9bC1i5qzV0KcJEZlEAkI4qDIgojgwghGVeDD8NLS/sO4QMF5vmUvbzzW9tG1K2YWzg9OlHH4UqprKP3eWRcGT6emBu/HbyxtzLfz+pa9C+R/VOX5FXWytd4pIHxXVe8/qv4G68xpn9brCkgAAAAASUVORK5CYII=",
    "crop@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACo0lEQVR42q1TS2hTURA9M/e9pCFWW7FKFUSDiEQUaxoLatGVguBGTBFiXSgUwc/W5SNL1yLuBbPoWwjiohupYESJTUXRIlI/GzetWrWtL3kvM9dF+kltBAXP8t6ZM2fOzABN8ODxYE+pN5d+FcM/ggBgKDPmzqTea9uHLXtd7nhWk29nwtSnu3idNtg9Ic0J6XTOFgqkLYkWcb6ntBmx9Z+q9dl8sdJX/HN9SwDZ5hfHg8eT+45lHBNL1hB2O1oDIHvymUdH68rGYRUAUDK2DW0IbXX2ToXGVynK73ucjcXWlg3HIDZCrf4dMZOEofjvKmABMDn4GU3fKlYOXvIAKqDRJl3Z8Tb+tf3LcbDzAwi7XU4WQ/l5nUhGmhWRZSYmUYtTcbP2al2+7bhdOfzOg+UCSJ0bkztrAO4BwIW+J5vUOoCl53fG+x+2cmewZ3SeHHO5DmwF8G4i5xN8wGlMzbozKV/Dj9E6hywImszlhs3s7BqnvX2uvsBhAIh+NOsAMMGuMJsBoLsC8f0BIWVpzITV9wckCBLi+wPi+wMCNGKgJK2UMv4TWhKRJfZgeeN0F3uw7MFy5/sUe7BMVrkRo+zBMl7DAAseLUKVyRiohZ0vgBQTCJc+K1AAGDSlACCQMUFzzAoiYgmJmEE4dPbA0zJZckkjAYAaq4krSyRIuSQQ5e3nMmNTIVc7Zb7jJS0frGUAmNxfuhl3Oy+KRmByVrWtVhDpPFxOgolhOIFq/fNpauVRvrecZeK0SETLapWtshJkl2vWXItk7joZ941VW4vH6/fpbw5yxUJmS70x3lAOoulscby/snS0rWaWyw2bqamuFUUSicAEQUJkjuPsOMTsbDtyZPRFIhGYkZETIf3rvgxlxtyAopNV6z7wK73fFzv4BRm/KImTa1sNAAAAAElFTkSuQmCC",
    "crop@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACCUlEQVR42nWSvWtUQRTFf3dmjLsQBS3SBRZLCyF5WCiEzf4Blk80bzGQMoZUNqkeW5kyGBEsbRTy+ljJ5oXsdtoIWhgkhSJRRHEXs5vMm2ux2Wg+vHCYYZh7zpm5B0AUlThes3ej9sKdic1rgKSpGkD+wakyAILopY9XjIFHVmwCKBsbBtC/UElRc4ohjtcsQDLZ+jITtR/yP6kTJfWovWBNadkXvS6iY8Ae8Ht4QUENNjhT2u2HvaUXr2+sx/GazbLbhfPBbxoJK4BDdVFF3oqSiyCqqIqagPeqxS2jugr6MssIp8zNRO3d+uRWepbFZGKrnkRtna82Rw89iYuJ7dXqvABsd9WomtG02nQ74CrgN8BNg//QsWVBtXdw7kjRZGShkdd8I6/5wS9oaOQ1X4HhmW/kNS+ietLqcIYKkEStrwR59ulif6kCbgc84AA/3inPqYSnWi4uf7a+M5Z/U3fsYapWpejmec3ng0Y4XJOo1QHh+dbUjxPKCogmUXvbykjfh96KMSKqEgJYAwWqU4jcE2RezEj5QHtNB2gcZzbLCAZZFHHLRtwT1cICchQpkUNzumoQYwMLZwZp7ub7C4Pdd3pGXSmI3+/beiA8Lp3X8c7Pg1/Zu1r3WNMgu3om4cxk68Hs9Tc6W22WhnM+FvQGEkB0QDDAIPcqwfDKF3v3K/n0fkpqQPQPsBLzjZZFRP0AAAAASUVORK5CYII=",
    "crop-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACuElEQVR42q1UPWhTURg933dvXvKS1poskkWxKpQGHdTi4OImCC5iL4ogbhUUs9k61NdHa6V2sYNIFqGKVbqIIuIiODhVVBCCgnZxaFFo2jQ2f+/dex2SaKIdFDzDu8P73nnfd875LqENnudxPNK7vxy4731f1fEPIADI5XKRZDJpvnwM98Xi8dffy2unevvij/J5iEwGuv2DfH7Q+j6Z34kkAAwNDQUAMOXNfSWCYEhHKaWBTpIWrLVERLaDyPM8jvKOA67jJuphmA7CABbYe3383hE2oTAsNQCwMTYWc1ENaiUievvHaJP+nYHuxNYFKSS0DrFR/o5YLI6IjHR20XwKFlgrFm4PXz17YWxsjHzfNwBAMzPPotXiylGOyHUThumYE5urVKtTlvG8vSMJy8ZYTcwn4m7XpXKluPvy6LlFz/PY930js9ljNQBPAODmtcfbmA2I5buR0dMvN9NnYmJ2o4v5omW5HcBiJpOhn2K3XPucL/d0O1HAhon5+XmxvLws0+l02OQQAPSnD7WepmQdYjMALC0taaWUlrLhEoGMUkoXCgWtlNItB5VSmnlzJxn/CXLTnMCy53kMoHVidXWVPc+zgG3+vFGTzzdG7iASoSAiMjC00bS1fU0MAExO3q8AgBWi4o/+qukgClCpR8lhMA5Pj99dIGEjRjuNQIq6MNrRoQ56jTFga3feGH/wzVI9WQ7c99S+sADgil23uuLd50MdQgjRSmIzkgRjNKq1CmJRF8wMx4miWCqcpM00mp58OCBg+sPQUGMihmXLZMgYsn1u1B2u1CpTkuRHsK1pEX9Kf7OQ7ZiamD24JZFcWCuVBq5cPfOm4xr5HYOD86K/P9/xLpVKiUKhoBPOnkM9XclXxdLKyY1g8XEqlRLZbLZO/5qXXC4XWf+aOK4d58XIiCo2m7E/AF33N041gyTNAAAAAElFTkSuQmCC",
    "crop-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACCUlEQVR42m1Tv2tTURT+zrn3tWmxgw4OBaGTQwv+B4KFro4vS0sFxxo6iZKl1xuF5nUqVgpO1mTyZXHSSYr+C64iAX9RHQptmiaP9+7nkERM2gOHc/jgnu87934XAISkxHFq6s8alR13cAuAOOcUgPyXF0IBQES4snKsSnlOI6sACNzRQR0kSXGOemFCHKcGAJJa81dSa2wP0EvJxkLqvlEpTZfqWdbrELguIucku5AhJ0BVDVNR6ajXP6s+3rr3Lo5j02q1CqshfAoMuxCxIDcJfAb4UWCEKAiohhDyEIq7gOwBfJ+mCCIiY9qSWvNIwP1HW+t+UuK2O1gz1jRnr07PVSrlDkmxcRybxcWN4ZBvSvCKc4cWgAWQA20LLOTG/pgByLOzqX+E2mq1gvfLuffL+RALw36ItXPvl3OSnLzF0RtyILvxG+DrbvG1CixYoJ2PFMyam/dFwsvC6rVeb/50aekP7cRqhoEd730+kAyMavKkeQoDVKurx2PMJCEiTGrNL1EU9bO8vysUgUogCyM0BYjbolgHdCOKopkiOz+0AFgulw2AAOimGlM3hdknaECIwEwYtNgTWCW1cqmNkuTt3KgP4adVnc8lO1kD5AWn5m50u99PvK90xg4NvXvpwJ2njYe79Td07lUJA7PLmNG9lzD6BKNM09SQlIL80M+zB0A7c86piPAveXwEBwhyboMAAAAASUVORK5CYII=",
    "crop-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACW0lEQVR42q2TT0uUURSHn3Pu+2oiIjGaWhAlQuFMbsxVC9vVpk02JrSXoGjbcnLZB4i+gVCvEEQLN0Frw6J0BhfqTgL/ICkyzsx7z20xTsyoVEK/zYVzL+f+znPOcbSooLlMz2hPObe9RclzBgnAKNPxILu23ONGYuc+p1adym7xrgguCx4gAfJAwnCAGTs1UUPXMlMXz0XRRi2tPCrtJLN/MRCaAxGg2czkKJF0huAHDEGEG9nMxG0Dp0eORDQQRaSp31/ZfvvlROZs/+SYI1oQHAHDWwWVGBV3wkYABKVmB69Lm3NP4IU0ypShobvtbXtdd0R0LwQbcBLPmq++JIT5ZkcBVRV8ELnvXPzM+3SouPVmDQoKMxatrs5XgPcAuQtTfYIA8rW4M/fpNDi5zMSBuLanWO0ysJanJMkRo99dW7LQ3ebAoDNP3u2zH3XRlR7lcIBfxrodKEgLbAVYZMAnJN5JAyyWkPgyZZ+Q+ITEAz4h8Y1Sj0v5T4pOC4aAQkG3KGkdJqzzQ6EQAiVtwIeCFik5wEet7TUBDDiAGStBtXG3WI+jli/XT8rNb1odpVRpF1W4NdKXX8Br7I+4uYDzgjfzg0rAlKvZ3oebYul569Pv0rywAMO9xVeRa38cgiEtCOvjGDAspKhECIJIROoPH8hpjLL9k2PBh+HmXdSAmmASwnV18XPztZcqbgWxStVXPsi/LOSxgbzp4o4FSytjS9vJ4p+6FiDvxtls+WSDDneJst9W1y6omMiVcca/ddDh5pmvylnnZZTp+LB351451Y/ru8nPRgW/ADeZAFZpDUyBAAAAAElFTkSuQmCC",
    "crop-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABw0lEQVR42nWTsW9TMRDGf2e7haAwIMgrS9UFCeUFWBAjCisDLKkjVVT8AVAxsTBFbB0RYuJPIDOs8EYGliKlA8yIJEgICJSC7WNIHn2JUkvWne58/u77zraAgAoMbN5o3V+rN3+Pfu6PoGegYJpHWLLM1Ihe5YwR4WnC3AG0zRsDaGXL9ML5YgVv3/H8LzAUSAAFRVgAUnicqgHJG37HiN1NGiYikqlyIOivowpRgWTMyjBIeLT/+cUrj7d9+lEuZZ0ryEpXNThUHgB7KhQzngpqFAkWdytpqA3G/QvL+NNqbA7zrNtblmtmne1W5jVv+HrZtQNv24wE4MtUg3qbtgMcEEo7hhqgMR1KRbB+KijCkUCaZn6oWkF0cWRmvjlREFMiV60iBmA1xFncG7dAzUKalIizWADI6fwAw/tvL7/+h6q0onm2+dHgDhPxCSCCJgGrEBW9Lpi7KPdETC2m8FoAZnNLzfPdm1btrmpsgtpjnmUUsUZT2ln6Zi+evX269FdDdH+cDc6c2Eb0WYhu3Zr4fTDuTxbKeua4T9Bq+IeX17Z0Y6N9sqRs548UuqCFeLwdMODcqeYBsPfh07W3cEOg0H+zM7YF5FWlbwAAAABJRU5ErkJggg==",
    "download@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACmklEQVR42rWUy2sTURSHf+fOTSdJSa1FURdasEpdqDStaCnIVC22iFQQpvWJ1Y07N4I7ifkDxIUu6qY7X52FC0N1ISSRRkWIUC3iW0qRmqJ9mGbSmczMcZG0aaWhuPDs7rn3fOd9gf8tEUQEAJxpTh08G35xvKhlqvReVEZdAwB4hBuu4FsAwMUL+kdQKQawBfBYCU1l3qogpgh4UU8gAiDLKbNYKcUVQMRRkFchWlG8I14VdHrvy5qe3fGdUZCzNLIIWERBjr7rceP5pmfr/y5++aEWlwDArttXW13/trcpoUdBHpgUYpJRkNfb/HR/KFj/3hLKVQDQtISyDMRgmpgLUQQsGPbdfGEqviawefBkc6qTCFkQfp/YkwgH5Ya441nvwLgeQUQ0lmwqtlJvHQwE3IYhgmgvuOYvgE0p/HWC5PisldEevumcLKZVrhUBQG/LcIMKtcMPGpje+tUzjB73aMvtYC2ahqpktcbMcFzzw4z1Q3s0eiyjaXGZSLa7p8LP+4SgkTvpttcCABSIc2pVbX8OuXWG0eNqWlzG0hfNGWSO2O5cquDmPxas7CIkmTzgXOr6VCUV3wCYrywpNnPe/ulCUXMAUzLZ7un6oBJLd5szPHk463zfd3+0I6PrrCSTCQ9guvlku83sTDBRAQuDxkyCiYVdPz6PV60MgA2j6CKW7jYBmABgGOQWtdGS+2FBJHyLICK2qmQNuZ837rjQNvwta/lFSJ33AMByJAGAKh0GgKyVFyE14Jk5a4uUoTrHMcdKICaJ1D3HtS4HfGtHLGsOKjmwbblsQxfOKoVgW4ygfxMKXn6WJPUDTLTQRj0c3xbw1XR5rrPegwMUd2ylNWYBCUG+KYunYw/Sh74smfDK/8zqUrT9A5N3KW77IZB9AAAAAElFTkSuQmCC",
    "download@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACEElEQVR42q2SO2iTYRSGn/PlS9LQiBVKERwUrQ6pF2JTsJYaXN0cotLWy1zxMlgpIvxmcFAKXqCTY00HOwgOUrqU3mIRUxVsBrcuCq0oWPqb/Em+49AqwYoi+I6H8xzOed8D/1MengHoS+Zbz6UWpvuS+db6er3MZvwWAM4Qi9ot3WrMboBipk3+AqsUE4sWoEbED6pfayGoAKyuxi2o/AEWHSvuD0DFBiWLSKjmxHh4Znz8RBlEfwOvTzx96MWuC6m3/SAqYgMRA8bVsmTd2dTL/p7k3M76fgOQSYyFPTwTtuxtbNg+3Nc+/6gSKVdRaohb603lbzTF9gwjHPDwTCaxGAbYZEJP+/TleGTHA7+yPOecO4jIm8ZwS7dfXbn7+NWRQalbXQB6OuaOqWN5X2HifZas6z08czUWablXqnzRWKRZ/GBlKFfoHADkTMdMIuzstpFC5+z6zU5GDNzOknXn05MNuYXu+37l05Wo3er75eU7uULnwKXW51FAQ85cc+jTerc/KGLS6Unrl6OSSbyLjBa6Hn4LPrflFo4OZhJPIra5yaTTkxbHR4UAwG5sH0Wrpamp41Wg+uOm0dddSwBjxVPBT0+Ss4IS+gkLumRD8ZO97fkhRZ2o0Y3YDSpuPRwngpqwiV8MamsTG7CKq81fV0NJJJRBnVVRQVVQAAURFYwKxqm6Z+oqN3/9tn/Wd5ho1/OcbkOXAAAAAElFTkSuQmCC",
    "download-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABaElEQVR42rWUOy8FURSF15rrEgqFeERDhIRCQ69RoJboqfwKjV/jB6g9En9ARFCIUNAICm48rvkU9nDcOzfxiJ2c7J0za9ZZs8/aI/13AFnkWWAxav+FaB+4LIhakWXf4HySdB61bVMGaitRYknpqU5xoZRGwiZFtrGdt1Cb2c7LVGUlirqBSdv1ok+FktgbB/oam58CC/nLkg6ApVBWkdRmOwdmJB1LWgtspakvQBXIgF5gi/eYB3ZjTQF14BAYDmw1VV129Z3AdpBdAxfAPXAE9Lf0FDAKrMYJldjrAnb4jGNgoGhDfMUKMJ0SrQd4MO1XkO0BJylJ5I54ZyP1EZJeJT2E5Byo2K4Bc5Labd+F2jwwz5KuJL2kRFmsx/AIia9qkmpRv5aMUTUlegoHTwBnQZonzlZCXjwbktTzMT7RtFHglp/HHTAC2IBtA4xJWpDU16Ck6ZIj30jatH36YYVf/We+DrneAFtbgj/dMPaNAAAAAElFTkSuQmCC",
    "download-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABIklEQVR42q2SsUpDQRBFz7yEqB8hiqQQrO3Uzs7Gwp+IIFYW4gcoWtla+AUWWlqKP5JGCzsRiTF5x8J5soQYEBxYZu/u3NnZOwP/aWqVvqs+qN3yvLRqRp4FYBNYSRwzyWoA7YTvwBj4TNzO+x9rlyAiBIZFkhZQqVVEfEx9ucmoLqu9JknGjCOiVnvqUhnflNtRK3Xbb7tSF9WRuq4e5/lOxnV+U/sgAx/V11Rc9Wzyzw1hS10t2nSYhDr9eVOuuqZulOS+epP7+aKCN/U08Vz6a/WlVPspVW0DoXYi4lK9jYh+/rG5f27EbMhzwCAiRsCoaF0//XBiFloluQ/sqhdADVi0si4mrAL2gftyao6AAbCXCWNiHM1VA3fAyVTl/2Jf7lD445Rc0/cAAAAASUVORK5CYII=",
    "droplet@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACp0lEQVR42rVTT0jTYRh+3u/3m9sKLfpjGJ6MkrY1258s8TCzQOoWNFHXwII8dAkPHYrYbztI16Kbl4KCbCuMLiVkc+qwHSQ0MyIsJMEysvDgz839vrfDXE6d0qUHvsP3ve/7fM/3vO8H/G9o0AQABI4O2gLe5LHCs2IQm1OFAYBYEU8h0QOAtrqYiqthEQY46Bo5wEJ8IEBCSvuDt3VTYYAiIPkvimjWM6oQiFkgJEjNkFCzLBAiEM96RpViAjYQ+XxxpXvUu9ziGrpUaq4MGlLvNAy9s9RcGWx1DV7sHvUu+3xxZUuD/f6oAgAB98iVy3VTHPCmevOxgCf1vKPuM7e5kx2FuXkohSSxWLPR6h6+VmapvK0v/3iSVqcv+GdssgEN9GV/2TOToVRst1SEDu09v/i4/8yw3x9VJidjvMZcAGipGbK3145x0JPqykWYVlcOQU+qq712jIOukcOFtQIABnwDuY1KNw2Znl2qmg5pYKEhTAAxQKxBExpYLFVNhwyZ/m4IvlFYu2YE2jzJmTb38KMV09UijVABoNWTvBfwJL9pGudJaH3XLESqsdUEa9CEAOkM2jGfemFasSDf/r8e/GLO7osgIjcjiiAiGbKawF/vvjybztUSVADs8w2oiQSyYH5oLdkTbnbGHdHEyQm/baIE9kkDAPDepsQSjkyzM+6wmnY36pmf4dxzB5REAlkqVNTieFNutlrGASwspOfqe8eb5grVnHP2lZeZy5MAytL6krNn4sRKnFbbqmksIhGSzTWv67eV7HolWS5npX6dwOMgVpiFXRXWW4KEaTEzfzo61pjM12z4tBpYREDSf6S/2mreeQfETaqwACBkpQ4w9enp31dj7059zOdu+vsLE9qPjxw0sqoNxIqiqO/up9yf1ufk8QfkCh6fNh7RAwAAAABJRU5ErkJggg==",
    "droplet@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACBklEQVR42qVTQWsTURD+ZjfbpEoNXgLexFqP0u1LK6aFBP/Dlm42SUuh0UsQFRFtYxB6UenNU48iFrJ/QbCYTUxoQqgHD2pFD1LQgnox2Zp948FuujXixbk8mG+++b558x7wH0GDKSaAeDHhjLgurUmE72y04nt+Plip/ElNJjdVAOh0lNSJyOklVXaTwfw/yOw7IUWh227vu2RFvQ6AUqmUDOCDYRhlFQAsUVvPxrfY1F/MZeNbbInaehAfmLMEVgAgJxq3Fi+85rReuwYA5kT16uXEB87GG/d/N2D1iAO/Y1qvPLiS+MiWqBUAoHD2TRgAMqKeW7r4lk3hPDziwFc0xzfH5ifb0hSVIgCUks9DwdOacO7OT7alOf5yrM/Li6bmgxlR3z3s7Ftj8pUyor5riWoRAPKiqYX6U4OizD9VALDtWe/wDRBsmyUAeLzvEqmnfE6fTIQvCg2P5EXz2NczwoVtw4YhDdgKDODk+2a4S4h5XvcTALQONA9u2RldmNpmS3eW/7ZGS3eWF6a2OSecUZ8XAkgaKKuPWzM7c3qleDwcW82IRtTF/qMhVfvWk25U40ghrEVv/nD3VjbaMzsGyqoN8ii4Ltue9dKieiNEw6uMXkSy1yVSIgq0bo87K09b02t+3cDHKIGVeyCZOV+NqUORSwCdk4R37HaePXk1/dnH/fpfcJPZSHtvlpcAAAAASUVORK5CYII=",
    "file@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACTElEQVR42s2Uz0tUURTHP+e+OzOZiBWN1aKFQQS2MDQhSBoVgqKFtHgmFkT7IIJo0WYacOcf0SYY8yGVBC1tCiUqFUEkCNxkGFqRDuOk7913WvgzMbVd381Z3O/53Mv3HK4AgAqIttc/PZBOnqophfNqE6FAClgCwMRV6Xnz7eOz9xe/+36fFwQdjk2SbFZNLod2NQ3VWyoGjNjjqg6QdZMSE2uEIfE1dIu382PN/dnMoM0VWqN1kI96AeK6GodeepI6H7nFOx4JIEZBBNSJHhOh22CXk7YqtRjOXesdbenLZAZtYRVm60ABBDnqtDyRH7vwiC26deZ1WpPV3UuueHc5Kl6q3lf7pLPhFb2Flr4sanJIbDfsGoFJZjKDtmYubWbTc/FaXS6ZQ0msEuv047Fz7TebRvsr7OH8jbPvpnMfZNj31TOb8wK0UGiN6iZPR5trFIZLijPGJLqvNwznl13ZEzEmjsOHAAQBhh2UQ2JQicfnPofRwn1P7H5E2kRoCl1pATgJENDhLLtKNADHKD1Az9qqXG8c7hWR1jWXYY/yffVWarA1jn8DBYE4gNnZtGx3vmfQbvr/QJunpoD4vnozUyPGP9G3bRbFYtnzfVWdeiuC6jYgseDKq6E6Rv56uQPoanwTgl3vt5MbI5yxUtnc2Vi44pEsKU4ET/njyW7Fq3GllcrLTsujAD7q2bosSk5FzdADYKDSHnkRx+HOKyqG0JW/iIvvgUpdFpWtH9vBRG1bpOViLKrbQRKSUmNSSz9+fZp4Pn7151rvb78OC2PJ/YJUAAAAAElFTkSuQmCC",
    "file@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB9UlEQVR42oWTz0tUURzFP+feNzPPHy3MFIaEyHa6EHpB0qZVm9oFz4WVtapFy/6AcegvCVJiZuGmTcugFKyRCDQisY3gQiIUsXHmvfttMWpjoB74chf3e+75Hu75Ckwge5C8L3vXdy9YVgohxyQ5U4hU7G9btja/cvNtpxdABiAwpZNLcbHNp8jF41n40zKTQ2YAkYsLkeuhle2+et2YfNL9gANZ1NRI0V8Yz/LmffzOEL35cFyyQXl/KQvN9Va2s1EqDDx+dGO5VmFWlUpHNALwMgvWNjP/a3757i5dmL6+OJCTvWy2t7/1FIbf/Uju/J6v6lma1rz71yYJCmBKks+FChUHJmStyPe+MPz0fnt7P1L8dCb5cK1en8pdt4pkBrLR0Y0As4BMpucOvjuYAH2VvGVyVwEiTkEVBYC5lVsLwALAzMTiZYpsygrZmeQjpGnND2yMunIjydfDx/4IMHIBuPPI9fpUXm4keRWF4H3ovjuXfBZOjG0mdYIDXSdr1AUoCk6nKJt5aAM2NpYacFw10gBY7qMDMFPo/JIDyE1yKii47CJAtaoT3nSYZWhe8a6k3Lm9w7FNWby02WrvrXoXL0wni1uY6X9/koJ3xfJBtrs6tDP4BUzq3iqnvofASLDWkf/j8EiRnPmfOXtv5hq3t8D0F9He1r6oUUFWAAAAAElFTkSuQmCC",
    "file-output@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADMklEQVR42o2UTWhcVRTH/+e++2YmyWQaKqNG6kJFhYiayUsVjZikaKWFIi5uY2Y07ly5UMGPjbzMwoUu3JSCxG2KYa5SFyqCYDIap63NpCr1u1TBCqFTK2G+33v3HheTSdISg7/VXZz3O+f/7uESAABMALEa/jSdlPvuD0yV2SHCBmSYXdknQ679cWLlkV+UYkdrMtgG+WCRBzjnnco4Iv6RK3putRwB2PSAYWFtCICahlszJ8pjHzzvrbhz5dGwWyM+9uYcgBjAi2AM1oMrhwJTn2yb9QPG1A8EpjYZ2NpMZFvWIuS4HNBTmS+m5sqjoT++KLsimUzexZ2uSITcrCyce/QzXMdz+8/cTDJObdN4OYiqhwd67lh4emSJ88WJQicRWblVzgFBkFIFBwAuX/6B7q4doX9uv2ij38M9CZEiQvXqfPnhJ2f2r37Y46bfz42UwvwqnVSq4GyKCLiJgYEhrTgPsgCoiFmg7EF55TUy61ccxN/NjZSOhKYuHRETDLwB4OSQVixuLFZ4Q/UdmN/Lg6wPX3TSEvuYJV0eXY/QOkgkTpMQjxEJz5h2E0AEALNAx6EUOwCQzSxPZb0vx3ywGB9flN2Y11zhBlnv67NZb/kbAPDBQvhgoTWZbGb5pT29ty0QnMfzIFssTkZaH+3uCvtgoVTB8cFi63fQ5lkCs5jOlF7ojaffqbb+LFpgYXr41J0OMbNAKmheOq9/PBrkQRa6032nCcWF4YNewu0/1gqvRszRA3Gn/6dELPmrdPt+S8YGyzK+74nt8Td39Drk3tr693+n6K2+WPq1elD5ObDVt4WBhJQcBs121E4tAoDWZLEL8tiFQwFArz/jnRH9iVteqbb+Ss2fGzu+Qy3vKuqMXXDm9YOvPuudbQjBa2rofKw3XRGNSsXiHpghrbZLhFIFwkUCwLvK/xfT3lencyOl1e71y+4TkvNWBl3XHWy3GlWHdu5kOCCHYmxEuDcm+u4LTeMTAFgaXxJSKQitYRjh8bi44SkrI9DWelyDgx4AgCtSsCa4BMd9E2CamICl7kRZr3RvXCYfaodVEBH9VyQiyUSy1grXPtffHq50v/8XnUduiDAf0D8AAAAASUVORK5CYII=",
    "file-output@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACZUlEQVR42o2SzUtUYRTGf+e9d+5MOZmkBpK1qhZahF6DiT501aJaBRM0U9mqReCyRatpgjYtixatglAL/QtalaAjldMH2LcU9EEFKjiZzcy973taWCEV0W95znkO5zk8Aiogmg/H2jzTcNBpnHTOoiIiqmokaFDsu6Fy5gYI4AREAQRUspnJVBBx3zepzth9q6uKQVQBfJNMBF4j1Wh+cLCcOV5ATREURA2I+lVpD7w1nbGtHsZbaGW1XZ9KarN4Cy2xq81Uo/lXyUTTseM990aKiAMAFR9A/NhaF8VgZofuHaiwgnw40Wq1dqEazT5OJVpu5cPJoffpO/19Y33OAPjWT68K1vmirgFUwvBqIptVD1RQM53wGs8rXu5bfbaS9Nfm2r8GXUVkWVxzVKrR3PWExmUQDpVP2Y4OFARRPSnKNSNmOyLPYld1WBoByGZHPICj3eNnct2lUf7BiR2lDf07H2o+vN8HYEZHj9h81/hAY2rjRdAlAEWlgBqAAmqyHdNBATXWufRy3wqAyXdPnE4GzZcWax8iRPpyYelzf8/U3KuwND2w+WWyiCidT20Rcc7z3MpLfMXdrdsvbwMvvaluFx8oPEc8Iyqv181siUDpGEX/ZsMffrC3nO8Z36+aKoG2DJd3n/1zTAUQ3xlZWTXZ7Ig3NLXnRSX6lFGRYm/vbf9UOJX4+UiAc6CAWs+vgao4a37E8//JdU3sSwVNY0txJXOzvOuuD3Ck5/a2QBsmHHEVsKj+sVREnGeCtlpcedK60PzoVzyTEn90aq8YMWnnLGB+E6qK+IKTN1C/eXlmaw1UvgP46hgI4k2yuwAAAABJRU5ErkJggg==",
    "file-plus@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADDUlEQVR42oWUTWhcVRTHf+e+O/OMcRxbOtFAuyjUCikS6RgQLE5SECouiosXS6ZQxIUbPxDUhQiTgexcdlcKCiqJGYoahLpLnpKxVE2oluBXs2q1NDY1STOTmffePS4mk0xLU/9wuZfD//w4nHvuFQBQAdHj/V88kks/0bMerahNRQI+0ADAuExuxfzz65c/PH8zCCa9SmU4oUNSKqkpl9GRgdl+S9eUEbtPNQFky6Q4nMYYUtejpPb6+PyRc6XCtC2HQ/EWKEC9CpKM5GfPe+I/Gye1tzxSgENBBDQR7RVhzGCbaZvxa9HSyxNzg5OFwrQNN2G2DxRAkMcSrV8en3/uI+7SK099m9N0dqyRrL3djNeOZR/Y//mJwzNMhIOTJdSUEWe37RqDSRcK07ZnKWdu5JZce2+um91prOL06qfzzxw/NTB3rsvuGT/59MWr5R+lGgTqmc5+ARqGQ3HfwqG4c4+jqKEkxpjUWPFwdbyZ1D0RY5yLRgGoVDDcR2XEFQrTNmsz16J49T1P7IOIHBVhIErWV4HHASoMJ5b/URgOxWHr+GFrtUalmK9OiMhQ27cjKOibTN/I5dze26k3wfTuXtnzQSO76n57aEbDkLijHTuDRkFlYbgJUMzPnlL04dN/HnwXIAgmvXvl3BtUmPGKt6tFK74f60YX0DyZv/CqauKvLN78GKjdFySbpV65tcvX1PpY2mb2xlEDg8G32bP1eDnKej1TQE0Q0dYItp5QJ0g3h/OTn/trXfVsvxgOONwvjuSS58UHuhpm32cXz//V8qruVJECEgTqvbaIOfvToVuCLI/kqxugvWeqT14BeOPY7/71zGisixdEOmAdILGQ1CsVSYDkzGY0ZWU4ism0Xae/OdgAGMl/F4HdyrcL21f4t5XuIyfy4Yse6fW2IYmpi6gr5r8fVJKWV123le4XEq3PAQSoZ/tKKGUVNbPvA1Pd9tGvnYu265RWG1XdHbEoqV+TxL0DKn0lVO7+2Hal9h+Ntb7m5M5mtpUSX43xG8sbf1z+6tJL/7Zz/wPmbF9+SImsZQAAAABJRU5ErkJggg==",
    "file-plus@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACXElEQVR42m1Tz0uUURQ95733jZ9akWWCZEVFi3AhOUFSSqta2C4YF/aTFgVt+wOmaNO+fRDYIM3GRRDtCtTAHwlBViS6KHIREZbmzPe9906LIRuqu7pczr3n/jiXgAhQF4ovuq1pPxflW2IMEEkjRsfCtlx+sfLqxJMGFgAoACAglgZepoUcs86kvT5uZhINKAGAM2niTCsy//3h2PzA1eYCBqBcjT0Fu73Xh9p52LU9aAtdaYt209pOH2tLmV9bbkk6rlw6PvO4jNsslxukDgAspahckv1amRn+jiYb7Z/uCPB3a/mXt61J17MPxTPfKnd4o1R6bM0fGEkgAcRicS4pQ6ZUkgWROdt2S7CjP/MvPx3T65eLk4er1ZFgmllICaAOHVqOdwBVqwwGummA9wboA/iatPI0BwHA4X/2BvbK6edJfWPq6NjcqQkAEwBwuW96Lwr4RCUeAMz/cquLI1m2lu5rNTteXTo+dawRFUN02wBACPyHORIqQ+ZD//SwgekMsSbJnR3tn9qbH64+5VJPAJItvGteV5D3H09OtbNuJ6wp2Dxs+MS23VOIPlnt7oqpche4lbzVtiTYaFoeTA/+CDHsj9GfS5OdTtK1oHCgMjn0jetK1dAO/ppZcrQZAIwvDH4OrL0LIV/J/Y/J8YXBzwAg11oDJMbGlRwABJEJE+Zmc9fvKSpzQ8sllI5UUQ1/CGoHrNnJuqmv/6VtzlpT6PWxvgo19EsySjK/fWsK3SHmi7vWdhfvLx3J2PxVhu0XAfREZZBIIlIwIiXS0ciuBKyPP5o/vQqIvwCP8SXMtuUmnAAAAABJRU5ErkJggg==",
    "file-plus-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADCUlEQVR42oWUT2yURRjGn3dm9vu+3ZZ2gZS4FW/VAxzk4s2DcFI8mGh2ORCE6KE3NyTUENTMfuw2bKlGezUmraxJYYALIV7lYLxBAraBGo0xLXgwsoR2d7+/83rYP2yK1SeZTL5M5jfv987zDKErAsC6/GX+xalX9jWbT1imCcHtrYaAMzIysfW49eBc/eTfxhhZKpVSDElozQIALta+O5QvTN4LgmDNdTK/qFxuTcnukJ63FsXhj9l8duWCv/heqVRKtf5BDYPUwYMgAJYtX2DmfLu1+YGUAikAYiYmYrJUAHGNGbvHRndfq+ulY2f9w0ZrrXzfTwBAra6Cuz/HL8RxtHK2cmoR2zQ7ayZ2ZUStHbZPtzutNyf2TV6Z1Uv4xD9ltNbC9307KI8ZCYEcrbUCIADY/qxS7BGeZLbYmNHH3/lqzlwfHxtf/rx6aePMZ+//ZIyRog8iAhHAvVKT4dmmrdCmVighanPVxnIQBpJAImWqAMDVq90Td5Tv+1ZrrfZMRg87wdbHKqNyBDoiiF4Lws5TAC93QaVU4X/UbyaAeQDzzCAi8MXzjcsADg9ubSeA1sYBJuyI2viIGQVvfO+nnrduK5VHDHTh1PXfzqBKpcJEFAHAXLVxkoCxcvnoDAAUDUvAf27PDqBbsl5tHHeU6yZJkGVQ9MXs8odJErnt1a+XALT/E8TEBACjo+sud1DLZXP7n26GEELAy+a+2dpK4ixyNwC0u53iZxEZBhETA8DMzIm2TL1XiaIpWP6Z2d5VMp1KpHypg18f9YzH/1oRM5gIZIyRzeZtMT1dbAL0eK56KSBwYbr87m8AsLDwvWuMSX6/HxHoGUwNGVIxc6eX6kGyrcqUKE539b/L5aMhANT9b2NBQm0PLQj4M+O4r9f9xbelUK1BzVHciUF2vtp4w9qUeqeOOE72rSgK7gBAsWhkP7QEEucA3Bgf23szSZKBQ4gI2eGWMECCEMfhQwucAUAHDhSZtj9s+cL+I2kab1ob8/PuSCCEw57rhn+t/7HiL5x+0t/7D2wdY+uqGlQLAAAAAElFTkSuQmCC",
    "file-plus-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACbElEQVR42m1TPWtUURScufe+/XjZCAkiBL9QsJAtRPwPgtoJu4jfqCjYWCiugmbzYkQN7B9IoyEqkm1SCCKilZ1oIfhVxDSyVhqNZpP33rn3WKhxUaeeOTNnOIeAEqCOjU0NxTbaI0GKCAEhgNYyWBdVQiavzw0fvK+qBACSCgAOAFqt6bJfSh+RpooQMgDGGGpQwIFRuVLB+NjtSZJHVZWqSpLqAKos3lkXx3F1abm7d1H4uFQy1pg+6Xa/kZo9FxHT37fqSOvq3XhkZGQffsEBgHNeRUSp/lOSHF1AD8ZHpwZE8ivzXz6/GRxc/TBf2Dx/4fKRU7XatHW/SSRJYyNV5cTEhOt0Or5arXLu7XJWKpbP5nn65OvC527BFU+OX7k1fv5yfdb0umhQJakDAwMhSRKt1+sewGmQ70i7jTAvjTEKcNNK7L/x6hVss3kzKlmztTF8eAbADAC0xqbXAvhAFAQAzP/ESVLPYltYXyn3v7g2OrkdAFSVEtIKAITg+Y+zMUabzaaJ7Zbd1pjVeZ6qM27njdHJte12+4Fz6nv5vYVBRCWO1/cx0xkXOZumXSkWy9fzPMjsbL7GeZM7xz9mK2WpgiYUG40T31TsBu/9nr54lfM+PxZyu/HixQPzQbWkqv8VK6EZADSS/Z0sXXwrXuZE0qeNZH8HAAy5rKoKqFmJLWIZxxGzLB38OQgkj7+v1Wpb2u32yp4CbCxHBUqafgcAAspWq10KS9kz56JqnqUflSBAEBoUND+JGpwrDHkvrwv9gzvOnNmVsferytYdJLBORPDr9qlKJVWtNSTdXNdn9y5dOvQRUP4ADSk1K0VYhPwAAAAASUVORK5CYII=",
    "file-plus-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACyUlEQVR42oWUz29UVRTHP+fe+2aqSGsIb0oT0A0m8hrFjStNprLCuCCxXmviwoULN0ZDoiwISZmkO/4DQ6Jxg3asPxoTG3ezcaeJylRMKokJBWEqWpRpO+/de1g8hj7BwndzFvd7vud7z7nnCiUE0MfHjj06low2cm6qkggV1MWmW0n/woXLX/7p8bZNO1TPZZZZ06KlWeoPW7GLiDkAWuWgKGhExPwR4+Dtbu/zhSZN16FTsO3EW2iHyYb/RsQ8F2N4FwwQURURURXVCYzMgRlYk9TzYnPml7WF+aqYg2xYfl8M8fzy2mcfchee2efToMlcEfPjhPzoSDL66aG903TWFuZh1kArum37FAi1Jk3Xo2FSrsVhvJ6zR5yoMXKpe3X+2FPjry0kduTcZDpzqdtrfefx1lT7BWiHTrFMVlSjKcwWqCHqXJb6cyEWFhGjxNMAbcpm3Aet2KTp3PrYaoj5CRH7MHBEhGejDm4I+kTJawfHA1A2swM9zgBnFERAs4b/BOSFIW9HoQxfS7kWe43GOwad2Bz999TYymNxil+V7bHLA4RO6zIyAMjUv6EwurKy9D6Ax9v/y9hBaMpmqX/diqlH4kOqDCbHZ94khvpFt/ERV+jfX0hKq0+P1+qF6pw19f0xbiBicJKczSXkA5MsAn1BRCsbYO7dBfjp6rd9NuVwHjioys+K/lgEc9DgDry8+sXl21TdyZEC4vH2Im+Z72/M/wVyPUtf2QSZ6PY+/g1gP0frnt1FFxUQvUdIwKmycXurA3wAQDD21ZrG3UPeEktbAId0OjeIqwgtD0d4xVj7/JN7/UtWzU1cOeFIsRGNjZPj01OgQuEILuxymrwYtfihTPXWzZJpC0SVkwYW627kayWClsXsnZeS3AmWhKhhFcx75VeUqdz9sT1S23VEJP8nVO7/36aKipqt9UH//O/rX/09zL0FNFY6HiT0Y9gAAAAASUVORK5CYII=",
    "file-plus-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACJUlEQVR42nWTTUvUURTGf+fc+3d8a5FpJpqSISlDRLjpE7TIVcpQRFC0KGjbBxBp075P0CbMAZMIqkW7dlFBNr6EJEQoTpmQL+h//veeFqM5A/aszuU+zz3PuZxHAAFssP1ql9NkxMxyVCGCREVbo9pcae3Zy30ugLF/kEs9hcbNlPcqLh8tpAYKZgAqPlHxhJg+KZWnbtc+IABD7aMDiW/6GmM6upttvm0KzqXeZcFSyfljHzDUu8b+LKbFUnnwelU7Yb7qz5lZtKCyvvT79R9qkO8oHI8WHmZhZ967ljf5jrmN0s/ivQIFpzU8IYYEkGGGExjXAgWHkHpteIDIjUrY2RHVu/mOa2eLFEOtGBE1wPrpjzBhRYohIvcRFkEvCHwW1FTsDIDnCJTA9fXdSpq3t4bmy1MzwAzAQFuhu1H5EUUyAD1KPEcxbd7ePJ343Meh9rGLB8ZUaK396LrOghiMa77zyxWL2h4tmDi9nO8c6y6t6SunhFp+nTiGkJ078akFy8049S5YJXPiHwUjO9+bnMzS3Qp2KKm3rZZbXH+xmQXXayGMeM15I94JwfXNfn+6ESuusY5eUxvqU4DF9ckV0WwhxrgcQvZucX1yBUCTsAtYNNF/ts2CiDQIca/tYPzZ8vQ3KAzA9OGcGX3inUC69b/dXj3cX4sgelCLuC6zOLdXPjW8xONU6lIlDTcReqqhsX931ZA4UWS5EvcmF349XwXkL9L493pE4GgIAAAAAElFTkSuQmCC",
    "file-text@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC80lEQVR42oWUzWucVRTGf+e+d2YSktBUjR8LF12IMBQrmRZKUp2mEhRdFBdvLdNCsQtXghS66mYcCPgPuBLUQCWJGfwqBUHQZNSJRW2CEIpQ6sbaBmokkziZzLz33uMikzQT0vrAWdzLOc/l+XG4AgAqIHry0Bf9A+lnH68nNbWpRCADNAEwoW+gZv7+/ctfRpfjeDoql095dkiKRTWlElo4Uj1k6b5ixD6t6gHZblICQR2G1FLi19+eXDj2WTE/Y0uVEbdtFKNRGfGFXPXrSDLDzq+/E5ECAgoioF70KRHGDLaVtn2Z9eTeG1Pzx6fz+RlbaZvZLCiAIE96bSxOLrz4Mbv05vPfD2h631jTr11oubVX9nUd+PT04CxTlePTRdSUkGDut6sDk87nZ2ycXUwDFFHzVu7XVMuaRwSrBL39yfWjJzeS5c+77WOTZw//PFRCQhxrZHbyArRSGXHlGwdb54fu9d167pvuD64fTlySNBVvjEmNnRmcm2z5RiRiTAjJuwCUy5iODCoRQGFw7kN1S6uSfnTlbO6H0aV+brdc7ZIxqR5ETohwJPH1VeAZgDKnvO0wEtVNQ50NuBUIkjh7s1J5wQHvtQuAM7m5KREZ2TrbXVwDwMTC8GXgMkCcXUyfH/qxz9eTKOo56D8afb9OqRR24NjDaCta7qfxbtt/rulWQeq0Wha1BnF3KVx9+bsJSi9BJ5a9owX51mlrWdXXQE37kYC6XhFzZ3NJd67sA6Md3Y72IEm793+iVce77P5zLbeKbF6heFJRL82kNjsxPzzysGgKEMcacevabAjuX4KvebO5tAYJXpNe4M841og/rgFtFJ1GYhHfKJfFA+Pt2lvzUMhVm4LZnrc37jO7a6Xn2Olc5bUo2A1vgjESaQdA9WIkUoLrstL9qtfGPECMRjZbRCmpqKleAq702CeuhpDwUNCRIfGNv8SHi6CSLaKy+2PbnzpwwmljLYjqXiYpyagxmeY/GzcXv/rt9ZWt2f8A01parwFwmWEAAAAASUVORK5CYII=",
    "file-text@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACYklEQVR42m2Tz0vUURTFz3nvfb8z44/CykqwRWgtEikdQaFACFrVKhgoNWoT7foXJmnfonUbzSQkapEbFwUtdGp0IiRrYRqkEVEWTjM68/2+924L00bqwN3dzz2Xc7kEhABlMP2iRav6815swnsHIamE3jBsiMW+G3/dO7nVCwAUACAgzPTlkmGMWaOSHdZvRiJUoAgAGJUMjEohssWRsULftdoBCqCYCltD3dhhXeUitG7e0PpwjI1man3A+sqHyK4vJ4Kmq1d68hNZ3GI2u2VqAEBTxEssWvTaaL63iBoNdE83Odjblfjb+1RwcGoxfe7n+DBvZDITmgAw1DXTrk1qMfbVsypO5iXJJiWVzfuzvT8G0jOftUpY56PngM8YJuoglfbRwpklte1AakDiog827u4x+1a8d/MX0nMpQm4SXFJAN8B5UoulOgoAZhsW8SCCOgUz7MQ+BfBpstCzAeDRn8Jg58tWJLFCCewumASU0DqJe2L3qwuCEwPp6UuAQkI3NFZt6ZVV1WcBUxA47oIBwirnCJ4E9HUh/NYlvRNAAbJmBFO1YaratQ1N6lihb9hKqSMO0NaAsD0OeVwrdsZtq1lhKiXi/4VJDTAqL6Zz90K193sYoVhGVA4jFAG9FiwfGfHalUm9A9cE5sQ7Y5RydyxsTghPgEKIc5beI4/IaTFO6J3agZ2QAQ2BzZaxudNPALzFf3T51Ey/ViGrqlL6AwttMrcaxaUFrROPB9PTXwGq3ZhQBNbo8FDVFhea1/e/AYSs/SrF+iEArV4iiJB/8xAhDZXojw6lhw8K/V8A4W+1SSWKQTr0UgAAAABJRU5ErkJggg==",
    "folder@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACGUlEQVR42s2TMWhTURSG///em9cXm6JS2klF3CxIawNW2kJwcHAVXodUEd3toLgpIYtTJ3FxKS4VbGYHcSjFGIuQqktxsh0cbEU6BE2b9979HVo1lhoRO3jGc+75z7kf/wH2KQgA10arPa04vAqvXqEla0IK+pD1eLhx4r1fX+9jf/8n7W4eGIhULtMDAKOztWzQ0rNs0DfWjD+DJCQhm+lFM96ozNZHJjrvIgKU6xaPwOXGvrbWboUhH8RxJgiCpNXcXLuQC48+nszXpgX/xIu09B4APK1ChGhpszFb5xIAuNTHNEhE8eNMbbzRNmpucrh6qitz+DbBm20kAAgCkGUPisMv7s8ujU65nQcEGABiPl939Xo+EQAu8c6lM4sz9OYYkAAAvFJSxtAw9TIXM7Z7qjiyeM+1URdA5XLzAigCKEGm/IorAFb2onP5dPWLsea6Ehx3nTCWQV+CzHJU4a6SBZD6VR4ESEPJ/ckfZdCj8msuiuZQqUykk/mXKex2zuyXIf8PIYE/2O0wkgCgUJh3AFyhMN9RoNFo2igSsFpLAcmLdIlPTZe19EiThYVzCb4bpnMkAFAcet7NLktj/BavDL4+lLqtd6QxqY+fGppAOxvu/R1xm4jPOIbnveLGAbiTBIDiYHXIBtm7Ujzk5S1/3sJvxCADJ9Iup3HzxqO342/aD+hfsG9rlCATRXP2b9tLkClBBgC+Adxg2sFO8sRWAAAAAElFTkSuQmCC",
    "folder@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB2UlEQVR42p1TQWsTQRh9b2Z2V2PTWlAhCiIIIkFBklJQhCpeelAw4ArGgzcv4s2Dt5Cr/8MKXeihoiKIWrQ5Nb2oQUWreLBQL8ZNNZudzOchrYSaKPpg4ONjeO99b+YD/gjhRsEtBwBgrhaXvHUkNxWDs84lIDVJs2YlvnGnztc9Ako/ZRjO6igKHS8XaqXAG5tLbPMhgHUREaOC4wKHZvz58Pyb8/EwX8ZB9rfTZntm+cT0ZrNUfJDbgV0vRrN718rF2jcCCoAIIJoeupLeU2l83SggFYq+MvVk50egtefLbhXVj6yWJ55NepK5AGLcOUuCJEQc0hFfZ6+ljo+NbASgU6+7UDtpezMKZ5a4AuDWILvl4mIJVJNm8DSUCioKU6fUZme19Za5kUPyFABiKAAww8KoouqwUHX9jAAEAMqFRQC9IP4bv5RNsk1VIKoBMA+RrRcbiJhHKI0wIt4DpIgRIQlxuXN342p9wv1VLgLKheddgTaGlI6nM8G7+TOnLx2b/mQ8RZs6+f1DKFrlRGt7gAz2dV3ng6HR9203eel744+cWACA7w0W9QEYtR2J/fqKDG4TAML8rJ/JHjwqVkYt2kMewcLAgNr8+N5aWY4aFzv9m/OPEP4E6Bm7gCaE0uIAAAAASUVORK5CYII=",
    "folder-open@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC6klEQVR42q1US4hUVxA9Vfe+1z2fHiSahSGQia4UAkPPR2OMM2AmJBBx9cT5gOhqsks2IbueJ7NRV64McREIaGAehCQGFxqhX5iJI9rtByJZGPCzMFFUZpxP93vvVmXRdPsb0UUK7upWnap7zqkLvDoIr5t0YNtMIau3fyZIPAeBgQcGlm91/vpTHIdZEEybzdGfCky2CkNAAdIWULD1jzY/xZkOf/321D0GiAEV+LaApeTe6Y2XTu8KEcpqU5RQ4uYdjfZf2JHnQlzLHn5ugbN1yXwPfqrk3uvIvfXjUnrvMDk9TjZjzaykLNrpraGF9Pr9qLJnHlACSK2I5JQyZZjL31fe//uphjfGijOlvLc2VOO+UgDkA54qoAZttPHf0f6ZT05exNUgmDaWSRUgglJHCcp3eyvm0YZeiSJyJ6rbD470nP8h5+d7krTGAKAGRM6pNW1HydERAB8jCmCb7ZVUQpAEG6Ypivrcvi2XuxPUPM6SxaXF+XPo6mok1jKDvHWa1maZ+EOANAKcfZ7AKApkpDh7zFJ+gsSAbQG5AgNNfXIAFFCTwbCP0eLsNyerH0w8ARIxALC3d26gw1s3sZI8OKSQv4iJVfgZ1YiFVVSYzGbD/pfjA3PHWkBEjZ4GmKqlD6+fqG77+lUmHOub7bec+yJxC2/YBghDGY/HB+J3O+3bHy3Ubu0vQfnmYNm/CWTPA3QDthvIbizqeOqWkWKlagFAVcCg9tttcuedpbu7Oes8F4IEMep4wk4rhtAQZrQ4s5NILkWV4fkGEFSJjMbxkAPol4ZrlVfbtTLKDEzK2MBcF6vd5KQ+BQCWlciQpUQS/+ndCUHyEmokBjAiw8Pt3jperv/zGwDQ/p7f30yNf43Y63JSv0aA1Rdf0xqOoKQA5UyhmMpKtV3N1vWVU46/u7LjvmTZp1D8TGgYlABa/TT2imBSJ9lxQRJ8W+lLw6an8T8ENYktD5Z5KB6S164cLDPisjS/kf8AgZ9KrJDHjVgAAAAASUVORK5CYII=",
    "folder-open@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACXUlEQVR42pWRTUiUURSG33Pv/b5vNGemsRARMrFNGJWNQtFkfyBUtJGaBJWkTVCtg3biMmghRIsIWqWFQssoFFJyhKyxECncKC4yg4Z0nL/v597TwjFSMOiFA5fDee455z3AP8VUflA5tkjdbPlo5eHeE+RcNsYFkSSC/O4b99aLT7S88QHxJpBMDsuRkWsaAKg7PtXhWNGXbrD2BkCeGUbJUIKNzqz4X0+Mzl7P7zSXMuD6kr9WGpo5eWEz2Xn0bYNt7ZqrtQ+udsdTWRARwKwoVPS58HAo3Xa/D32CuuOp20wYsKq8mgYg1z8xboB+k2x6XV1ZVdthjFetWQsiNgR1SJDq1cbUDaZbVhSXjXByYe5PtwYdR1I1EedKu0YRns5mAJkRTMQQBsYrSaui10P2LEDPqSueugPQAKnVPd7+XN5ZrJ9wZCRRClZZkPzjFAEwHDBB5kAkjeHTAgCIWA5OX8pai3VXK6y9iaLOtDuOiQq/ELNtP2LbfsQIjnooxAz5zQB8ouCMEkTEgJ9sGY3CiCcF98f00Ezb2E4O97Sm9gmosBH6izLMDALZMszwzV2t1GQyOSxjC43iV+OC2YRq15vVbPGbNlluh/AD2zYpxUxEYPPs/fF1InoMAPgAANBIb2mqAaArPnmOoeefTp1aV0TsWbLS6Ym/O995bHxZCIuhhf6bMpZPwrdYKLNbInQxYPcBAChS8lWg3Tnbio0pDjaqxfZNQ4AFCFIItDtPUjwCmAgAkk3DdmX4wGEOOBKgBEBtgwMohADI/NLPz7MTSzdK5esx4b+1wfwGCC0I46e6x+0AAAAASUVORK5CYII=",
    "hash@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACm0lEQVR42rWUy2tTQRTGf2fuTdImtqBY3WgVdZWIrwQUXVRRRFwICndhWhFFfOAfcXt3/gMiCCoudGHAgmsfqE18FzeNgm6s+KqgFdPa5N47x0WaGrXqyjOrmfPNdz7mnG/gP4YAHM0/Th/IPzp+aN1ITyvRv768vX9DZS+Aj2/aL5lfWTzvigH4aqfXdnUuOVN3G7t+oOUUwnmAgEBbRecgUhkf7xFAxNFMPfwSGRu/aV1QVBT7oqlI5S+KRFuMJnaXKrEbC2+BmerSK5jXTUWDzJz/TtTXd8u9c3tb1ETYZWFc0y5S7wAG1jxNCyxE9SWAn/XcdgXieeqUShIX85VzSZPxGnayBjiodhhxuhX7UUFFVREWq8o06ETCpNOhnXpweWTLTh/fuNlSU56g10E6RZHmkp1K/Am4KQgIixyTWhzb+j1VeY/BGJXhf85AMT/8rJiv3JjdFyo7jmx6rgOF8uq58ObPVGa5iFR/NFSzUTxNMqmv5hw+H98EBLZ/fXm763blIlv7gmpGxDltNb4GZqgJtZ6I7FY4IfDNlc55kf329tLI5iFQcaveoFAKwMixjkS3Vw8tiBDGk7hOao8jqT0AkZ3GakjKmXdGsSSdbsL61ENgyGdQZGbY9GDfrQ47Nb+3YWtxAt2SdBdcbISfB4xwHyBCrgnEYmUfpEh3dunniTcfStVtNX4ZztnYv6HSf3jjqBYLd1fM+ixffl8sVEp/elG33R5+djRBNRe9pLwyjGuaijJffdS83lzO1OsswOqYjxqyoy7VXBSAttxg2u1RzeXiALGI6bMakXDsRIDYqYas6u5YnlAYCxBLz0cbILbNUj+3P1sabCbEXhDMybNPCiGoJMQdm6yPn7ImcRUguL01/m+f2He+GBy065glbQAAAABJRU5ErkJggg==",
    "hash@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACCElEQVR42q2Tz0tUURTHP+feNzNPZRTbRNGmcC3TTLqINBIXRdDuLRwjWrWtPyB4vEW7MIJWbaKIoAShjYtaiOSUoaJGCCFIRUStjEydN/PuPS1kZpG282zO4fD9Hs6P74HDsih6YQGqlbfReLn2+mLfdAFgrFybGKvM3QeIUdPCt4OY2PRunDKggteStYXRYr4jByoijBjMEKhwnv3khMQ/XDrTBFERjjiffp5cu/AHREXpVFgG0WRWshZHWj4amDmaqwdpWJQsTeUl0I21I0Yl5737gvKgEPbeyfxm95P5s98BFYDqwFwpoHPZ+bqC7lUTK6oeBS9gBIPi1ZpQMnZOP1s4txIAmHR7PcsH1wJynRlNEdUJj38jMCXKCWvyt51P71nJf/K+sWua2+v/3fp45Z0bK8+N73X1vnR9cFWjymLPvzgBuNq/0qXBzmXE/FLNjgc2fORcI0bMvOIuGbG3VF0E9rdBerRZmH76obQdABBu9QVafA4eJcD5BtbmE8HgNcOrw5pwUhDA4MKtErAqbYEMzp7skmLQyNIrVoK73qfD3tsfxuoE0O+djAa5Dqn7rfrk0tDXg+ct125Wy7Xd9iXKtVfVcm3mIGxbJDcqi7kYNQjDIuy0ciIyKMLPGDVR9DEfE+9X2LGliksQ79FvKI9BdHN3Q0AWUJlKEA9rLiHxh/JIfwGxoNsXnttEpwAAAABJRU5ErkJggg==",
    "heart@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADN0lEQVR42pWU72tbVRjHv8+5+b1W7cyyDR2T62RQTLRtEqkzJANldL7oVjhBam0jlL0QVNg/EPPSN74cVHw1aNBchqjVqVOXYCsKabdL5tw6vMyNMWzd0h9rcntz7zm+qHc07ab4gfPqnPM9z/N9zvMQNpGHZEiXWRlABsDlSEZqGjkAwLlUuhfKdDmSkd0LZUIlIwogga3kIRkeiKSNhX+903ZgJD7zLKRnnKDsBgmLJJs8PRv/FgDe6K0eARPDBBYWUs7btvnRx3rq1/tCbjq/r/rfD/h2nmy26gJSLIJYKOTb1dlYX/wMAEL+8GDDurMqpfiDiB0IeB4LrNtLp56efeFtAGAFkJhf8b7TEdx30rJXPhAwd0/OHdpjNTvDa9bCuKJ4BxXFO7hmLY5bzY5wce7FqNVceqLlrLzX4d/71rWemXcLIEEj/TMRsnzXHWl/WZzr51t9GI7/qAJAsZoyNnzJswIKAgBe7/vpU4LnCDG5zyMt1u33dATX7buTnJeULkNlH87GW4AkzjVW1DYEOC8pmsZFASRO9FW9ddUQZNAprxI6ZtrLcQaQT0IIMDJLGhd11finpCQ1LevkkWd55JmmZR2AJADUVUOUNC6g0IKEaDFQiDnCvmTZq1Laoo9AsstQ275BAQXhpuJiGHVGICls65jtNBmo9fOGD73TE7mkLoaiUyoApNPnPXgInJcUABiKnlNzSV0M905P3C//tej0ox5/6BeF/MGGdTP1iT5wPZ0+76lUDtttIigpGrLOq9HJrnCgW5cQDdts9D9Te2mZAUCxlqo3nZWjIBEM+vZ+N9Qztb9SOWxvjozzNpEpImXnPfPu0WItVd8WLo+ePTiW1G/kkhf/4tGzBwHgRN+E1xXkz3+1azRx4bc3k7V72ed+OOT24ANzPx47ExlLXriaS+j112Lnetz9oZ7v948l9PlcQr/jPrI5YmoXk4qmkXM8dibyiF/9GqBoS64OQMqml3V+TqDGmnn7Za02cHWrh9u62jWUP1kKBvY8VQ56H09KKWDay1eWzFuZLy4N/uk+iP/CHQ8jsW92jCaq2dFENftK7PSOzRb8D7bPIPmQuQQAfwM8unr1We1x/gAAAABJRU5ErkJggg==",
    "heart@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACaElEQVR42n1STUwTQRh9M7straJiQpDExIMhMXpButufCLGQeOBgoh6WSIu1aYB49GAMXhybmOhNPRCNetSA7M2bB2xLagnQKmmUqHgwnkwV0GKwtbvzeVirxaDvNO/N+76ZefMBAABi4XBKRQMEiBsGKQRijbrjc7RNG4kj2R0euW/PWq30ZaKgf67rsdCLvSpr2l21yx8ezofKdZ0JEH8XmGtmUr0Ess+risdjyQok2dOSftxVmPucyr19BAkprQpnyk2bW9c65oPfeBJMki3Ht7tbx4issVrt6zFpWzHOuN7i7XgEsC5LbhiWvaET7Ited+sY2XI8CSZZVM8eUOB5XaPKyESh5379SoZ/rn0n3xWufl9JPSh2l+r6oJYddnHvvZqsHGQR37MLnCnXt0HdubZfq5ZKadbW1kumyezfjQxSYAIwgPb1ZXX108oqQV5WiUgSk1W12cNhmsjAsAFGAoKnw708nem1mclsgJgwwcohDwdQBaHCuUud4lCb1sqrx00M2KNaQQWAJJIyk+mzGBgBwKhWUJNg8mP1/QnGeIvNXY8BABFfVsQDxcqgNnPSMeZdjV9Y5wNd00fjgWIl4ssK5z2YUgBgSJ9/mgguUaRzpruxQPwaHqMzdTjuX1wf0hbSTg5TCgBizkLwmD8/mQi+otNduX4AMA69dDsJp3rigSKd1Z8/6e+41SRAHH8mj1idnNHzd0ZCyxT15QYAIOrLnUoElyjmz0/+7W0AMSGIA8CQtnBjOPSWotpsZjj0hmJ6/jYACEFcQHBsDWL1DCK+3NW4f3E2qs1e+c+JWzfZxP5R+BMC8xJgQXBQFgAAAABJRU5ErkJggg==",
    "heart-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABmElEQVR42o2TPS9EQRSG33PvtQkqX7slEYlGI0qNgig0au36A/wBv0Cp2ESrUtBo9BIaDeIjKhoRIhFfIXsfxZ5h3NzNOslkZs57zjvna0yRAImkJFaZWdOxVJJJwvfczHIVxUnK9AZYG+zHxwrAhKRlSTVJn5K2zGzfsXlJS5IGJV1J2jSzsz+sQAas05ImcAc8+33XF647Ad79vuH+SSBbdWAdGHRdBajzK3Wg4lg/sOb6lUBSBd6A7TZ1GAVGy+oC7LjvgIAZZ14EUqArKnIaOaWh6ECX3+fcdz6TVJGUS/rwvdUFMyQ1QwRhDFxyX/eSviT1JJJOfTam3PnPGJhZ2bwkbrvo9och7AaQh1oAmdpISNdrlwONuP19wBVwC4y0I4tI+oAb4MLPyU8XgDHgAbgGhotkBZID4CXKICkajftLD8B41KHMz0PAuZNMx75lYVeBS+AJmIzwYU//MXok61TIKnAMfAGzwLQT3HYkKSHrBo6iL3IO1ErTKf7++BuYWQ70Slpw9Z6ZvQJpYTjVKTL7jy7IN9eF+130n25XAAAAAElFTkSuQmCC",
    "heart-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABNElEQVR42nXSsWsUURAG8Hm7mwiCV0kQol1AC4tAykgaG0sDcrnG7tqQIvZnIVhaJv019y8ERGLln6Mgchfw7mczey6bu4Fl3nzv+2Z25k1ERKCgiY6hQo3Sw5s+1l48wgEe9/B9vMQgNlQY4DPm/ttXDPGtg82TN0DVJpjm5QVe4z1+JfYT73CE88SmrfB5AuPeHz3BGfZ6+Dj5LwIf8BcPc0AN6p6g7nwP8AeXTUSsIuIuIqrkLkspsqcq42VOuCR2FxGLwNOsPMoqO7HBWhwjrPCsvZhggbebEnSEJ8mbrPtJ3z7JcU/QpD/Eb9yudblddb73LBO8ScJu+leJ3+TAqvWWZYKS5+skDjM+zXjW53b7Ku3W4EsKvqe/6mxjFVsmWjoz+IQf+Li14rYkG5LeE/4DK1vCwh9VCxwAAAAASUVORK5CYII=",
    "heart-pink@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADNElEQVR42o1U32tTdxT/nO/NTZO0UsU2bogIoSAM0y0kdfcyQzJcqbXYVm9vHnzu02Ab+A/c5XEvPgqFvfYlF9nq7FwnsZHNTW2ga1rZlBHUvbhYrbVZovfH9+yhjbSxsn3evpzz/ZxzPucHYRvYskSpVBKtdzYaZbJtHwDYNJVSrUbZaJRLtRpls1lJ+bxEO9iyBHYBA8QA7Wrb9meHw83UmaNBIScVQQeY4QjGdOLOtz8CQHng7BCEPEdMPSC+35D+1+mF7+6+JmJYopQpiXBz71f71Y7zz9xXkhlPiCjSGwzteeI2ZwCgVw2NrTqvNiTkQwHq26t2hNZc5+Kx4fc/Qx4QhLwMNbo/PxTqOr/uuhcavn9AW5h552Fd7XnsNCdVUsZUUsYeOy8nI3W1R7tzOf5MNA++cJ0vD4Yin/56dfELQl7Sb/qZqC/5gSd59sOFGbNdh59SIzEASJdnqwBgwRJ5bIp8e2D0G4XEkM84JFwp3+sKqGEmTBdMUyknk2pL5IJpKunybDVdnq0WTFNhgPLIy3IyqRZMU5GCL0YCgbAHTgmfKSgZkoR8adq2rMZicqsLnLNtn2EJhiVytu0TwABQjcWkaduSOFCTzC6BI8Jz5cqG57DrU5IAjlWrO8aAkJeEnfOyVq0KAtiDP970PeFK5RYA4NbA6NSyPiGvxDf1mM9kAngLCqapAMC11EhsWZ+QvwyMTm2237LEz8Vyd5cXvB0kJfyosZEeXpp7MJ/JBD6+ccNrJ8nZtj8dH9l3tDO0JFk26qqjHz+RWgdjczrnE6f7VvSJ1Ypm/FlMnDrcnlkrkyvx4/uWtLM3V3Sjfm2roy2O107F1NCRimY8qujG6tXU0BEAmEom1Rbh9x+c7F3SjN/v6kb9evL0R60d3LX2S/2D0Ypm3FvWjbVS6mSiZS8mTh2u6Mb9Zd14WtwKsj3jHbvGpqmQbfuX+gejfZE9PwhQfMNzhz3mZndAvSwENWpu/ZMT5bl77Rq+sdUFmEoOtl/QtHCM3y3tVzuOMRjPXeePv/5Zz46tXP+7FRD/hdZ5mOsf7FzUxnOL2nhurn+wc7sE/xu73aC33SUA+BdMu3uki5gEcQAAAABJRU5ErkJggg==",
    "heart-pink@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACZUlEQVR42nWSP0xTURTGv3PfK31NgKiJaGLioCZGJs1r+9rSWEgcCIH+QV5xclLi6GAMLj6bmOimDkSj7iJvMDrpoKVogGILRjGSiItuDUEsCC3tu8eBPFIVznZ/937nnj8fAIABysZiKhqCYQk2TYUBauTZWEx12V8X7yLxlsOe1gOlNWfFX3yy5PK5kHnIS7S37JS/h2Zell1ODEvkg/lmD3zXJPiypqhaRdbhsHxdY37YBHFJU9QuCUZdyooQdLcmN24ZM8aaIGQk4B3Z79WG6ySHy/XamU3m8wLkP+prfQriUxuyZq6x9Evmq20ebRjcNELISJrxx4+rqrKw7siL0ffPH7sl5QM9B/d5WmIl3sx2TD0ruXwykLjgU5RH67XNEzQdTFwRwG3hOK36EV91vFSizrY2Jtt2todnmgpsACbwdXVV/bmsLUvi64KZpQRVtWafsG2gM5dzyLYdCxDuZLcS2RJ2O2srLYLBVWKuCCJlTCV4f/2u9qZhO0V9SAWADCC7crk6AQwARX1IJWTkj3o1oRDtEZJebPURjFvz4YHKRLA3CQAFfcjTuMKCrnsAIBfoOf05PFCZDMYtAMCYaSoAUDCSbxYiaZ7Q+zoaBa55soGekx9D/asFIzW+rWOAxkxTsQAxG0qNfomYPBno6waA+XazCQDe6r3R+fAAfwj1v7p3rNvLsMS28xgg9zBr9D/4Fj3H+UAyvbWaeGohkua5UGr037cNXgaxZQkAKBrJO4vRQZ4OJnKLHYM8Z6TuAwBblrAAgZ3CbQEApoOJm5/CZ6fygcSNXX/cLcl/Ve0g/ANMkw48JcDBIAAAAABJRU5ErkJggg==",
    "image@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADVElEQVR42qVUTWhcVRg93733vTcznXmJ1qYmNYG6KcQWayZgDflZSClI/Vn0pdMJgiuLC6UiuDQExL0oLrpyY6R9haB0pTHRNCatTNJYQiRilCnURaaZNpnmzcy7793PxeSvVjf6re7mO/d8h3MOAABgwn+exq5qPIi97LWONDX1RwjIsBb/vCQBxIARUsmUq+NgbPQmFRlMBDDlu6cPSzjTtnJbjdEgEiAIAAyGAT9CgiGFg1q0fkfKqPfzGyeKCiBm/nFIyWTrpl49QbFVNkrWTFDehJ2SQgrX2YOh40hIsjnktceU3fyT1vptAr2nts60Q1NhF6n5iwvd2nt2/JCbaT9ZjdfXRgs94/+mTr5repUg2rY0AojAAoruWaHKd19rl0jOE1lNaetJ5LumPx2d733nzWxBLacr3FIqCTyDuNP3+DeaJYDjHSAAIBD86z3VfNfM61JZTWt6pS0J931bpS/knp/9sPVGtnQRZAAAS8AwhgVwqrG6F2hbUAP8okQKqcj9QAjnZBzr3wMnDkZAZujY5FNOouXpVbNSGJl7JRjiUwJbxhG7QAZe52L6yPwLVwJd+ljKRI6AByFvvvH1TG/l3HNTOelkVkiKHx6n1sWhY98dYeLNLU/sMgIYdvq2GMFRgzlcYPC7BGIAyGWnzqTtti+rYWmyWrv7kbSSV4STmQJTwrAOH2IEAI5yGQAGBibVoOcLAMh1fT+YsVr9ul6fuA/r9OhC33hNBz1gRtLe74K49gjQ9rSUDgjfH4zPHp94NW0fulSPNiaqauX01bnuwOtctP2F/qVQ1/t0/KAGwNoD1DhhX6RoGCz8paPh2a7xvpTdMhZGldkyF16+fN2reR5Lf8mPPO+yvHSr/9d6tL4BwN7RiJkFg3kjWQ8/AZlzxycGEuqJbwzrW+u6+NLVn88HhPOAjxgAfH/bMsI8LDZx6Khm2ry31pHPzhyw5b6vDEd/BNHqmYOqPfSyhaaMUzMAUKlXRcZJmkpYb7MoldFcK+6kn2j6Cx1X30ommpcJBCUTqIZ3m2zhLlfJCIcNwrDxp0MZhHVGWh1EFFf/5Nh8tpP+Ro1825ES+1+LjT4cIyoToIhJMP09/MwSNgtS93VcHhu9+WKRd/vs/xfbX4RwguOzXvEfAAAAAElFTkSuQmCC",
    "image@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACXUlEQVR42qWTS0hUcRTGv+8+5tWolI9FhLtaBL28QprWJsRw04spmrGIQhciCtm6bYvsAdHGTRuZzUAkGCjtFMcJZ8pcREEUZEkmpY7jzNx75/5Pi9EaokXQtzyc3/nOd+AA/yECwkjL7E6/a94hcMATFwQMEEoA2WoCRBRp7iA5XzTcgUSqddUAKKYzc1/TjbMlrzBJiiaQDYIBgH5ApDyBFLiOyVCn6ZbuAbxqlP15uORtjsdftkcB4PrRtwe/fP/wbuJ9l/3nqpeapm4Reh8AGFs1m6C/10qH8ihNkbTqampXo03THWEGF5awZDaGq7SVlXpHl/WwYjlOGRYYINdzpeK+ULDB2igsdup6ME5q/SMZ6xrQ7G47x44kiUqYhAIk5BqFj0V37YfPrHks8GohpTGA0m0l+3169aGirD2CkixAHQC0sjEEwkAi07GuNOekzsAoRc6MZtqeXrZePPCbux46Xv6cpow0qZ8GJFuZGUJRABifa58HMA8A3c2pu0GzbjDnfjvvFhbH/cHG52Hf7hNZe/H1L+cKSa+VDgFAzEoOh8z6Gzln6UI83fok8eaiY5vq1Ka9PAdB9W+YVBRokYjoI5nmfHfTzM0q/56hXPHrlXjmeCLSkgz2WmOh/alW21P2BIBgxbUlIJB8IkEvas0OmXrV7az9ORZ/1RYHgETqWGF7tSinNZBGZeYFjb6umJUcCfkaevLO8icI9sas5DCEHgAoiqaJKF0P9HiePbkFC11zdtDv0tFodOSd5QUR8YPsExEFCECKBgg1U5RSz8T1BgDh33/lH/UTztQMLdt7qGMAAAAASUVORK5CYII=",
    "image-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADbklEQVR42q2UzWtcZRTGn/Pez7kzmZlMk2hTDbQuusg/IGJ11Y1UdFGGQJAIFrJRUQNGjentGxI7dlEVgpBotaF+4KQQ3IcWpSCIdCHGtKKVCJmQppnM3Llz5+Pe9x4XTaeJ0o14VocXzsPvfR7OAQAwM+E/1r1ZnZmJiLjgLgw4SeepVqtFSikBTTxgNAYxNNNMpP3QWyKiNWYmYmYqnPnqsGmJa04ieTBSEQQRiAQYDI4ZAP+DAjAME0Hgrbda9ORbZ4bWdCLiwtSlYdNIHNzxth7XyC5rJJq1cqlu2lnNcLQ0YO0RaQildIaIupN214/CaLxCRGM6AAgBM2jW+cDD0fXR0eFwevriob5DR47XG8H2m5PDyw/ypzC1cFuQ1g8A+q5hrAud/Bu+fvbshUdNtq5rupHpzubw/tTC7PjpkVfn5ub0UqnEAMTg4KBaWVlhAUFEUADQcZQEYeyDsYYWGi8YhpG5c3u9v7Kz/aFtOy/PzHzSVyqVlJQyklK28/m82vNX6hDdfehksmqZNkw7cVrT9eNhGN3SdQomJmRccOcfSaX6jmz5mz9JORqcm7rUAek0cRxjdraYaqjfL1e98ke6aQ2RIL8VNV4cHz9Vm3E/HzLt9B9Co+8yVvqXqXc/PcrgOjNr+4gYwNbWhpBSxgBeY+bXiYgBoOBePJk70Pt11atcrVYr71mWedlJON8Tka2isL2PCAAcJ8kA4LpX9Xx+UeyK5LtzvYv1oHbFD/0Tb8uR5UYreIIAdHVl0ww0/yV0v7bE4mJeTU9+9lw21/NN0Khd+Wtz/YSUo4HrFs0J+dKvrXZ4rNkMmiAyOkJEggHAti1yXVdImW/LyfljmUx2KWj4P6zeWn32/Pk3msViUZMyHxWLRe0dOfJbEPieAMw9RCziOOZ0OWpLKeOZyQtPd6d7lpVSP1cbG8/Mz8uAiHg39vhe/EQUg3Df7DiO213JDG141YFz8ote27G/jZX6c6eyebLfeqxdKBQztO3F6AG4rATlHopRu9NvW4mudthcAwDBzMRK/7LZbm6kUqmbiaRzzTLtTBi1jiaTmZs1069rUVjRMglPhAlPT6cqIvQ9J5u7oZSqgunjzvbfPSNzA1Yi+7xS0eFIRWUBTQdYMPG+1ScGa5rBQtMqfs1bmpw5tda5Z//HYfsb8Qi0FbYPbDIAAAAASUVORK5CYII=",
    "image-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACYElEQVR42qWTy0tUcRTHv+d37533VQeV6g/IXQ9w3ybEaFMU3UWFYqSBiANOoUE4jg9ExylBXChCNNosJogEg6Jd2wpsoEVU9FCGfMzjDjrOXOf+TgsfTW096/M53+85hy9whCKAKRqa88Phi0DglF22AbAKQILBFZ1SKKqXmJdhbXf3DNzOCoC4LFyPFEW5ZpftnwCvsLSTYP4KwiqDVxi8AiAl7d1PiqI124r7IRGxuj/1bLG0s9QXar0OANOTi6fL3358DkwFSv9bHQ0/6VeIOgFgH+aSIHLOhGY8puZ7K5gad4/VZScGnzbpq1vJ1AlogF8AsBRh+Zgl/4VBKjNME44G3VfVuLmRana6PHFmdHXMdtwi0O6B8tjgPBFQCUMS2CPd1vft7XzG661+LNmutdleJBBHhmNdbnfVmUI+Ow0gzyAFAMSeazADrr6+O2bRss5rmmNBMl/u7W95ERmOT3q9NVPFYuEKadp7IcQlIuQrlQFAAqAH4bZlAMsAEB2JR3W9OmDmsle37C9Lutbwxu+vP5fOrH08VK4oDoVmPAAwPjQ/oes1PensunGv/+bzcDhsUfr3hVwu/Y5ZVv2zMwCRSCQUwzAK40Oxu3W1x4Mb66mW+wNtz6LRhDufd1EeH4qecv0rELUfwgy4GFwwDMOOjCwE3U7P6GZ27UbvQGscAIJBY+fw2uGYOOAO/pxUhXZxbDA2W6X7200z8wvMJ8eH5ieYYO95kwIQ0uFwtFsl6/U+zKSW5wJwaJaqqk05M5MksJOJOsEswXsBAAkWisK2lC+lQ3QzMx0lVPgDfD0Usqfd52sAAAAASUVORK5CYII=",
    "image-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC/UlEQVR42q1UTWicVRQ99773ffOTNkObyUhiLVSUNpN25UbE2lU3Wmla84WACF0Jgoql+5ZsulaQLroXDV9pULqSUokIgtgu/JlxBJVAdGz+mzSTb968d28XkwyxoS7Eu3i8zTv33HPePUC3CP+9aOcgAFo9kBzmyLyiGkgB/reXyjBWTb9nN1P/++YcoEQA6ETl/BGF/cZwbkghAKhHUaFPoGHgpf2nJ7zcuP/ZnAWgAfSmoWjIaetFBF3hEGUaVjddsWRygft3A4h2mMgoiT/ANv+dFf8eQJfsdttY4LS4MHDvLq53nj/42tOFQvm0Eb/84+L07SeNWB0cXwBoGABslyYpgen+oVV7wp1/RhHdI7Il5gijleTjnxfS91/A23YfGrqICo8CIUVVgRoREIDdohIwP59uBfBbzKbU9hvDTlofMtl3j1cmK3cxFGYx62tIXYo0PO6a3eOIaJ3ZgslcZpjTgvC7D1kLmJLnDp47VIgLzy7R+vfN5lSLkPSI9C6qiupgsq++dPyGC1sfMdlJAA8lhAuN5S82jpXHJws29xuDZwdC309HB5KjADYVMHsYubDBwJTUFvEBoBcB0q6o58Zj2/epC62vvLirBtENy/Q1EfIKdf/UCIChePvTnLIJJhgARspvTERmf+pDdmfVbJ2pL9y8LV5eAgBrCv2kmu0B6tmKCqdIw0hl7Gxs+6aDtO+sxXKm2bzVqlaTuLaS1oLSySAuA1G0C6g7QlBHwBWuIXUj5bMnI+6bEW1/u2w2X5+fT7MEianVUp8gMb8sTf/qpbMORdzTSCFMYO2sPOWAKTlWHjsVmeKXqvLDepa92nxwqwUAO7anSLf7k5DuFpvgDOeoUH5weJQmBpnt5wL5Q2RzvFxUV+ak9JDbAgAdyXHEbQFywwZ2v5Cf2wEiA/1EtPNObIoNEIFh0JGsRJRvtAOxRoBFHgAQGUCRh6EIov4v5ehab/t7MRKbMYEegcoKKawCTNDH1p8UYGWiNe/cTH2tGyP4v4LtEfAqWLLGSghSAAAAAElFTkSuQmCC",
    "image-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACNElEQVR42qWTPWhTYRSGn/Pdm+Qm1krRRAquOiStP2RxcpFSkYK/N4NCEaFLKS2os6uD9QfEpYuLdPBWUYhQcXMStFCLDQhFQYqFpliaauJNcr/jcFNbHPWdz3ven+878B8QQPLdfo945rZAv8Ui4CpYQHcMWsHsUmFeG3a8UgvWXUDxuCci51SjV4AB3UTFA02BdBaoWNGmUTOIJ3epccXtrD2qtl1erM5cAjiWGz68uWf109LSbPi31XzOv4nqKEBMVkIVSRV7hzK/oswba7SYqu1e79/nDyTXehbo/ZZotNOGLE3WpEskjuN28rjARqOdPpRwvGIY1QYdEtPWMDbH1FVWaAFQhXyuJFtduLEw1kCm0eaLkfC7K6lHoHuBF4Dms/6Ya1JH2jZ8CFpTxSEuJzZuFe/zerBh1Z40xn0syNmPq0+e9+0v3U843gOrrfMi8l6QMyLUtjPHsIBUqsE8MA9QyJbuuOJNtKKfFzRryrLG66TTdSKMNj/sVN6CFnuHMjHRn0w46WvNqF5arD59VqkEzY0kp1pR/R0q3X/IEqsaH9+ZWynXC1n/RtLtut6K6sOV6kxw/ICfLjKUWV7Oh6rRrAjp7cIET1TrATNRX650XSRxK4x+XK5Ug2mAt8tBIzZWRvGNqLrbT2V1QcQ5XchenHJNeqRl619BDxay/qQKUWxRjUWsI85IFP9EXEA0lAk8miJmoBnVFxBSqIwiWOl0oYgKRq3Vl04zMd65i3/Hb6VK8t0WqRo4AAAAAElFTkSuQmCC",
    "image-down@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADkklEQVR42p1UXUxcVRD+5px797LL/oCNWwsBk9KEpGpadpGmiKGNxRIfrKbd7bJA0sSkicYYffHFxHZffdJEX3wxRgPiNjaiUR8oFCzQWBYRGgxJ/7TGVhFhWdife+8548Oytk2jMc7T5Ezmm++bmTMAAIAJ/9vKuUbZIY61jO3ymrXtjspDQP0rsIZkU/pQUhuTQxm6CjARwNTXMt1E0rxkGcEapW0QCRAEAAZDg+8jwZDCQsldz7JwWz7+bt8NAyDWYjphCaumYP+6X+uqFW3Ios7/uQmPTwopgtZdGI5yhSQPl2gtZEr/tKNKLwL0ulEuwJatN7iaApn351qd2J6R+mCgoaugsisDM+0j/ySxNzK5QkSNWz0CiMACBq2atpFs/bZBwjtLZIb85kNIRi68OzDb8crJ6Iyx5M9xeHlZ4BGo3ekYX6GLEtAuAIgKOoGQvthegJb9UpihFedq3YZ9622P4X85sW86vJq5psfHD7rpxbidTscVcBood4/+ZoTKEwAN/GgIH3xu8E0hrC6lnGt5S+WHEVexPSP1Iath5221lEllns338mGBrfmKO0Aasd2X/c2z+8/kneV3pKxKELBh8+aJ4amOXCJyPl5l1l5hwRMPiLqF3sfONTPxJgB5DyOA4fH/LFJ4VCODVxn8GoEYABLRiWMBT91Qwf5jPF+8/ZZp+AeFFZgAYClWxXsYAYBlBBkAOjvHjHgsLQAgETkfD5g70iUnO7oG45nBuQNflTgbAYOrPTtCYE33AVUsvPygSKfj6vje0SN+T/1Q0c2OreVuPvdlpjXf9/hUNyhg2zp/sKSy10GYv0taWUK1a9ApsEgtkn08MvKkzwiftdX6VDH/RffwUso+GZ3xFZi/lmx/OPD9gRMAdlaKi62FFMya173b7RRI9+wd7fQa4RHNzkKudOP5QE2X1d8681LRXavV2nEBLPREx9v6o1PJyscV5YXkkmWEqLh6szEZnXjC8mz7nFldzzm/HDs7f/T34iaaqszQe9oMfMbEJTAfkuQdV+A3AOAUQAJgMsCDjiqseatCS5asvWBKX8jV+eag2biYjE69MPBDx9xG8VaPR/rbCMLrNbd1E9NPpjCeAphSOA26c0a+2eU1t3drZTcpdlYJgkzpDWvYH3x0qS0DEPe1XjgqqfqMYnveLeae/uTyod8q+fivh62zc8wAgJ7I5OFky7mHASAW+1RW4n8B0xaZoX4m72kAAAAASUVORK5CYII=",
    "image-down@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACiUlEQVR42qWTS0hUcRTGv/O/9zoPHQVfQWSBhIsWpt6RQhsMRIqKDEOtmZEoyZVQpC1FaFP0ImjnJkgdIxcmuGgRBYbjVM4wuEqQxLSJtIeOjuOdO/d/WmiTtQr61ud3vu8czgH+QwQwna8I7dc0+w3JqVIp04JACgiSAc5UMlgIhQVpc6YZ7xmK1s2qADEpwdsCdDwtrTEiCIZcI5AdIBvAWw2ISEorrSq2RhKaA6BGdTvAPsNaGx2KeM4BQPuh9+Wfvn2YeT57wvg7qrdy/CZIuQgA27C0BBS1Q59ybiA9TkR6YV7BD2/V64YcckzHENP25rjE8nJRiihOTJC/YSYFhPh6erPM6SjW15ILxxTFESASnX1h/RLgNn85+yqDCsCUgYkgAXaaanJu01z5nqXlPWJYBeD0KEDs14OdWUruwYT59T5IroGEAACxtUgwmOzD4YZVKVL1CtkHiPnMQLj2WZv+5oFNy39oWhstqrBPE6mnibGyY2aAiSUACrw7EgUQBQC/O3TPoRVeSRhfmgYjNSN+d+iVy1ZydDU5P5Nx3iHu0KecAODTJ+84taJrcSPWOhipGfHpoZNIWa0JI/YShD9gJhA1N7PSF3Zv+Ksmul22Pd3rqZjvScTztP1wMN+m5o5JjeoeT1XXm6WLTTtn1iRbxvAwWV59sktT827FjQVfIOwJ+PRgMGni6mZ6ZYnADW3ut+P22ZKyDCwY81lK9imvPjHg1Arvpqx4TJBa5S+fKAaw6LLt7gFkjqa4Lku2dIh0AmASAJOUdB3AmIBamzSXpgFKADgLhXIHwzUt68bn/mxtl9OSxjzIquoPez72AvQPv7N1EBeqo13NFS8OAEBvLwsA+AnBlA0npijyBwAAAABJRU5ErkJggg==",
    "info@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADC0lEQVR42pVUXWhTZxh+3u98xxMTxMJoHHN4IUFkzrDkFCor8Vik0AvBCz2StSLSiyJMvOnt4PRc65VUEC/HyiQR8UI2WSgxuqiFxFQ73SYVodAxUoQq5CRpzvleL04zWqkte64+3p+H9+d7XsI6OGDhAgwQA8DZ5FyMjPZeAOC2sfTT828aYSSTA5ALUt1c2kgSOs6ZT04ziUuKA1OKSBQAfNXyBGlVYnX1x+qRWx/n0HqDbRYOR+mzKalFj7b9dzWGfw8KCwAAgQRBDhtyd8oPvAde++3F/PzQfDeXuo9sspCKGPGy4qAZBM0z07WBGWyC0VT5uJTRHCD0Zud9JjeXeeaAhQAmMZx4Zeg7em4z1ErT++fQdG1gxrZzmmUVZZfAsorStnPadG1gxmssHWJWDUNG7gwnXhnAZBg0kn58fqz/BWf7CikAGDcrugMWADBiln8YNR9NhSNwxLhZ0QEgmyykxvpf8Ej68fmwczCBlNvyVx7erAzVLKsob1T7Oi+Rp3CI9AXA+7qV3aj2dSyrKG8+H6q1/JWHIOU6cAR9Z5YP7hCxP/2g8X3i6W/X71vHRKk06GMLWFZRHivdVwvp4QtSi1xbFa0DQoD3CdKYNfzhwlXx+DJ3N7nW2sSo+egyADCYACAeX2YXrgKCeU1EWAToF9geYwzObhckFGhRcUAU4GsHjqjXe2l9AAPvAbwBgMm1f1ev95IDRwDa4UC1SGmYFT9Xv/27oxqLTMiG5W4EAbsAfLlGxF27C1cxqWxHeYsHZu+9FgAxWDgR2ZPJ9hVSpdKgP25W9P+2xnwXoHJINEnjZkUvlQb9bLKQisieDFg4LlxFDhwxmxjVe3ve/SVIGl5jKZ1/eeJf285p9XovdTdoWUUZjy9zPn8msL+6+/nO6N4aw28tr+w+2L8w3dlEIn4rCFr2/5XIBtGeSv2SjMk9V6TYObTqN2qA+lWR+h0+QBoyBPFp0W52RkbMJyc10iYUgrRGeoxA8LntCWxzRtaThRsJnWeTczGle/ulNEg1tddbHbYPUYqMw1R4ZSgAAAAASUVORK5CYII=",
    "info@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACmElEQVR42nWTTWibdRzHP7/f83/SJG2WQtkoFLxIEUZd0aTOvRET8VQ8Jpa5eVp3FETwsEvYwd287Dh3q8rW5+Jlp2G7MKpoU6RdVhh6qEIvm7JmtWma5//iZYUxtu/xw/f0fRGeq15fjJKk4QA+Lf/2lgQ5KRKNIH7HB7/2ffu9Ry/75EXQOLE8NZQ58k3AfkTwaSAIEESiWDB3DwbPvlzcqHYO/dpsBk2ShpsrLc3ms2MbIoyEMKg5+hPZmHFPZiKEQU2EkXx2bGOutDSbJA3XbAYVQOZm7h/P61gndb2bC+3yPK/RxXL72zjKX+r5f6durZ7bNEBQH1239FYX2uX5ej1Ejx/fk4k984Yhd8Oyf3l72P597NgHYSGR+c9m2tPqo+vAh3q+9PPbGR2u2XT3CgAPk6jVqlqXWi/IM5ca32pVLQ+TCMCmu1cy0XDtQnllSgP+pPV9O0RxHZBks54C3F6vbqVu54vb66e2AJ5zGaK4bv1+6pycUXGS96Q+PfqkB4R6PVGA8+/eL2Uz41sXyiunASqVexEQfLG76721qkxqiEJPiTV+cjR/WB2Ak9yOcwc2OJN7ITPRbrGgaoz3/KGC/mo0aw7oTgNh9/cRAxDZfmEoLhpRrwC57YkICAd0p43m4igKK/rD2ukHA7+3ZOLCNYDCn//ZSmXZoPK0n+4kgn1UqSybwjuTFsDEhWsDt7f0XftMxwDi1X1uNN+5WG7fXGiXL9EC4C+g8XLPRvMzA/anAJFmM+jVq+LnSq3ZrI7+6IPt+ND7yg9oTz442908/stoJicnIs18rRKf6rt/Pr61VrtzuDDqLEYJDfdJ6ac3M3LkBoSq966P8BQYVdEcr9i2vO5VGvR9URknuG0Hq6961f99AUd3zQSqogAAAABJRU5ErkJggg==",
    "layers@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADhklEQVR42oVUW2xUVRRd+5xz77RlZtRCfNEmRo0mSIztnen0AQ6aGDAxQZARbAuKJjVqVEixxGjE6E9pUXwkGPthP1rGYmvEH2N8DcUM7dS5KcFH/DBKrEFLlJR5QOeee87xo52mTSDdn+esrL3O3mctYEkZSiQMNzBUPmmLpKPtzlhsATGPwSIMAFCZIB4/wUdH7/PLF63OqRZOolOwyi1EDFIVjyvjH0q6zekyJh5PidHRDQogQ/NkBgAS+IRbkVu2MUP7A1a4zpO5KW28bhCrILA9Aeva2pLMTWoyB2X27MgwHlVlIQQA7Xenr6dA4EkY86LNgzdKVRjTRvUMuo2fl5sAoHZnfDMj3mXxYJOnCv+A6F1TKn00eKblPD3mZFcxyLSgQK2Gf8xXpu/jydjYYvkAsPjZO+syTeCsg4FvV8ab4oziQjBVbbFr7pCq+J1U3itDk+vOLSXYoABCPJ4SdxZC1OdG5MBkbCwRTf1RaYI3VVjVGy9506vpQDwlfivY+wSr6uQssMrzi98YpruP/hD7Fleotvrx9UT8ZVtUPai0d8HThV7/1r97F1b4eDxVoQtVT4Co0+bh20t+/mcD3bsCp5N9boff5mQeYSReskWwoSQvTgGmB/Ji/+CZjUUAoO2R0doKCr99Sf/bOew+8CcA7KzPPASiLlsE18/6uWmClgFRXSNVPmsM9Qy4kWEASKxJBatWXNcv1Ww37Ypm7mJU+ZMx8rzU/gEmZwbKXVrrxx3G6A0YZmut30xOxk4CQIeTrSpA7bKYeJWxwGpPztxLALDDORmzKfQ6Z2KTr2ZzhswRCRw5lm2YWjKfhvEa0uw5MvSs4BVhbeRXJT3z2pB7f2bJN2+PTKyFMXuJaDcRJ21UEn6pBwCYCHSBRKsxvjHG9Guod5Ju848LFmmPpNcKCn6pdL5jwF33BQDsiI3dwH32DCP2vOCV1QDgq8sXtNHvK6E/GMo0Tc9t8PtGIcKfKll8iublngjw0G0llRsB/MOD2ZZTAOA4H1prcM8LAPALTr/nuk/LOeXpZoK1x+LBhPSLv3PhbyIAeDj69cqwWbnbQO21efBmT+Uz2uhDR93GkSUzcsa3MWL7bB6KeapwjsAP5wpn+4//uvU/mosDuoJpQ3WezP9lIA/OxYS137ZCNSWZv7ppl4sRAPD15c+U8d+6Woxg+WCbiLZFJqLLBdv/0bDAItb7l0wAAAAASUVORK5CYII=",
    "layers@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAC3klEQVR42n1TTWhTaRS9937ve+8lNs/Gqi0zbRdaUCsqtI2poo0iMp1NKYK/sXUQBKmKK6toxYV/oOLCH5iNCjadhW6KoB1RcIYubBoSFzIdXPhT40IcizYpad573/uuCy3IoJ7V4Z5zOZtzAAAAgDGReGTAV9gRS7ftjo/1dDePrP/6/tnHCACAnwnyFw2TTelOQaKPyGhSwTRLMQuV9nJaB6cGcvFBAOCZQAQA6Fg0GHGc2p2o9VFTOrWeKoyowD3DpjtKym4lNA9bhrPK84t5JjxbKLxJ3XnWWcRtsfRSyXQLkRq0Vn8y84WBXOsw/A/JppG1hHgIyfiFOXgRoLedhA7WOHZ9o2b/upsd3zTz2N5wz0okHhntDfcsAICBXOtwfzbewVpditj1i0HTetyxbDhKpjghyD6otVIMfG6KChcHMxsnZlK7Yg+qQDt7AbBXCMvxVekaCTqMu1Y8qSzojxUW27606LgQ9r5A+yrQ7u+ag7tERrtAs0eQlCoop7RWfWUoQ0TPmSRllA9Ew9V5M0SL+rPx/WQUHUA+T2TsCcnoEKGxF0BfIaPopLLxLkRRX2nWvPKN8n7c2jJaZzLeCJlzNpS8988A+Egq2zoIAHAgka+9/HfdGwCAnc0jnYB0NiznLi55Ew+Vdnfh5s0sJv+5Pbs6tKCRQZ+zZeUq1598q9k76ampIdOo+JXQPG7J2TVl/+NjhKC3PJ0fezdvXoHk88e9NeGGlwDTr1PZlatdnmhEwow0Ilcr7J9eSCNyFQkzLk80prIrVwOo16Hwwpc/F61DpBnvMgfvTTl/PNk8OuR7IG9mWjoEURUyJQRR1c1MS4fvgUw2jw5Zcv64huA/LYI7CACwcfn9WdVmNAnMfbaM1rn+hwyDd+xf6P9rCXStQzBPWzIaK/sf8oB4yi29+uP22JapH3TbbFJBSRsiTFp7ueAb3Ybvrao7lm77Lf60pzuWbvveqj4BsnNOPS7FG+8AAAAASUVORK5CYII=",
    "list@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAB5UlEQVR42rWUP2iTURTFf/e9l6a2MTaDQehSFHGoi02HQoW2WBCczWCDDg4VnF2cSnGQzl38Ay42Cg1ubg5abBDEVijYpSIIBqSdEpPUfPm+dx1i1dbYdohnu8M577z7zjvCL6iA6J6Z/aHs5vzGQczDIGv/nMbGXrqDGNOZ+7FdDnLDxetWjsz5xvbgwtro5lRm+VGXOXo58LUqYP9xrShuk8kgqszlV0bvOABR80Hxj3ti8WpLXV6AdKMggmkro+JVI1TkDZ2EAGSzak+833DzH08HIDqdeReLl5Om0iwJA+2JyVK/Aqz1l6KlpYmws46uDBUvOhu/6SW8mn87UpkaKt6K2cREM6qVEbWyJyuKCipR3CVS38PKwtPV83kHYJBjKANBzbuf6scFMggexGq784UIEEGe/49lL9rUp5PmwUomBNGdMKbT47ofeXPzlaTT41ooSNRZR7nh5TNC12Sj3vOwsH42yJ0rXnCudzD09bL3akTM7mWrF2PEO9ubaobbr5+sjqw4APUy6aybh9oz4CtGbnTH+rKNJoht/0NUI7pdH2FUvQu0hJqnSveaG4nFwvqlLQCXaFzbrn67DVW8N381gjFdClBvbPElEXzu/KsBzDBjZpn1O6U2c8humm2FVX8AMe247Bwu8qMAAAAASUVORK5CYII=",
    "list@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABdklEQVR42s2TMWtUURSEv7n33d0oz3QuWlkKQgqToLAKCbax3UiMjY2Nv0F/gWCTXyBmC1+dUgxoFkWyIQipthKDYCrJ7pLdt/cei+cKUawUdOoz58wMcwSwtPQqazSOrChWY6v1wl/50hJs87l/TvyEi/mCHTSwolDkTyCA9fnOYxO99m6zfW/x7QOvM3NlGvTBvAyrRh1GtODzmTIOPrW7N55k35dcl6UAkIhzmXRHRkRk/BCekCmCnNBL/ikqz4tvLvvS9Z/tNw/vXt25VKvns6OT4UkKpVwZDCClUs4FC2G2liZfR8/3mr3Ks7mtmNlr4L6cNmrKb6dsjKhDmN6pYyTqvsYg8h64VpGVVnzp+lWm8eE4Hj8dpzEZ/pTMCRGnEc74+D8ENt/ZgtTd7N58tL7Q2Qguv1XG4yHIw7QkCBGDPztTToYfNrvNtWlJ3plcD8As7QMXDBmy06aNZEiG7f4d2b9+1Xlt/4awDBw0lq0oFL8BzJibwJtjRUkAAAAASUVORK5CYII=",
    "lock@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACpklEQVR42pWUS0hUURjH/9+59844OpFCuBiCMHBjETgzNG5kaBtFGF0Rr5kUgS1atWwxCIFt3bpKgogZkKBNi8D3A1OhsFmoDESJMdNzfMz13nvO1+KO7wf23xzO+b7vd77H4QBHislfU6IrMV/flZivByD22/ZLHDwwkdYAYis62XI3fmMZLBbBYrEz/mHZio61AMS+zwkg00xrGbTKtsbR3nBFZFCxWnHdzW7X3exWUCtVwfODbdGxZxm0StM8DPOLAAsAaGscvXI/kWUrNt130MeKTfc9SGS5Iz5xeW/M/oySwwIANKHdYpYAtp4DTGbDQsBsWAgATI4o9Ur2lGJ07o3ZDyoURDI5pEMh5HhF6GFVSiWHtVzI5lxoglPJYS1fiR+OLP4EUV0qOaRnCwVxzJQAq3H8aUdsinGM2mOTq1Z8auBgLDGYCMQdV0fqBM42S2/jDpG4qcCPwGQzK9+RRBmu+ohoyaCqlw6Kb1/NNud8Bpja4+N1AsGxoHYm4sp1SHYR0MIADj8ZR65DkEBQr8aW+/eb48nm1x+bvugAMfOEZeihyIZXaDIEfiuuFK677gmhGKgAYO+AFMgQJFTJXj2rB6pnNN58TKAnernMgCPX+PbF5tnWDEmcUu3R8TxBRABA9+sHEwS9z82FU+C1LEANwJENzyJDDTAZAJZpigCWO6BteUFb9YCUPwk6bnKE8iUWJrf32yBmAJCGq5lmWsvnh6m2Nn0kKJ//TLW1l3xbbhdaBgmdmdXAyLU/+A9ZsUkHIH23R+DvIeOcaI+OPyTSlhhEVM7y0AezY/MuBLRwxJEbMwCgp5ASX4PqRWnr1/UKo6afWZ0iF4YgHbZXfOeU7P4UWNCextK9xKeoknaNhHciRoMOYqNYN/dmtgc9CmD6B19wMBx18XjaAAAAAElFTkSuQmCC",
    "lock@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACJUlEQVR42oWRzUuUURTGf+e+7ztjg/YJQkEhE7iqxfSOzqJwRoxyFQQp6JgFLpQW/Qmp+1wWtIoKEqpVSLVQbATJ1LFNJG0mqbYZaeY478dpMdPoDELP7j7n/u459zlCjVRAFGAwuXhFTPSkhjvfHi+3v6yvA8guOGpgTPvct8cc0/TCiJ32gu2iYx1oCNXPeeHmtcl85geMCYyHNfAoaj71PJfol1PTtmk4U/I3u/Eo4BCP2Aff+OH2x9PLqS6AcSSs9uzpeWYBZJOLbUOpVe1NzHT8exCgNzHTMZRa1QH3XWrvfQFIp2ft5uaMRgrvuyzjvN4JtfUQfP0ZL8iRQlyB2BbeOuhwKf79YaEQN/l80quJa9Bdar+RXPGy7vJx6tTvzpcG3IXBvVkJqFxvmz6KHr6rqpmIHWspehufBf4gCIoiajtW49kg2NlQYUrk1+0nSxfXbRANwvkJx9hXvXDrVdErzYGJIeX/IqAqWvJ/fwAxjold9sLYBMhNuzJFohRuTU2uXMjyH/Wdm7sjWLcAyrBI0aiJpdOzdsXz66EWsNfANxumSQXdhVWNoprLdQau+0Dy+eGgHs6kZ3mU6wz6E/NKDQxKefHa2Niq5fO+UsBDy3mYihcViALkcp3+ftR4xTfGNCPiVzsLct+Ic2/AXVgDjYQoqEoNLaKCqGWiJzQsjlT3DKJZd+GSEbs7VN9SDaQ61C6rxkQ1CIrTT1fOT4HKX3D16pRglKXAAAAAAElFTkSuQmCC",
    "maximize@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACcUlEQVR42rWUzW+MURSHn3Pfr5l21FQjIhYWrGxoi4RUikjY2HmR+khEYmVrPTN/hgQrJDobsdOFKumkqRZpiEh8RVJBqDKdTjvv3Hss3ra68C1+q7M55/7Oc865AIAKf600108D0bhzcGOLaTtiZd73pFWaOnv/ytiOG6BSoCglSu7E1kqvkdY91tbUmExSt5/7yw/kGaj4AMc7KxvFC+4ZE+abdk49L5Bmwihwo0BRbvfuNgyVnHWcjcLsocTWXOiFJsOKc8e3j3RdHuWVAVEnrif02/KN5vSOKNKVYT1cE2XsPoASRR0a2mMBkpCTif20NpNx+dnG1GbPRHln7UEQ9QFEXdO6RjMwbvJSpbcKVL8xEF2MyiM760Ad4Ez32JOarTYddAAYADXeqtDP+QleS8pk0P/+AFRi+j1Qmc9VWwMv5xulfQE2KLyeb3y609D6WxBKqF3uZLm7MjhQopkVtRozN4Gn/AepaNrKH+1TmpMiMAAFinI4xqRd/r4Ox5jCHz7+SwnA0a7hvaGJTrvGxzOXJ/bPpsZEf4YijjH+88p5EXfr6viuqwbAE92SDVf3SZRdB2ictvwDy2pAtP3FeC4TrDwNXs8SI1HezdtqknUtcwBlxP6YlziAaPX7uYb9kijyfqmQg8CXMKjLTAdAoTfd+OXtLyre9CgEmHobboi8fCDo54VCKla427DVadXs6LHuyuTL2YdTfV3DA+lECxLH/R5AX3flYmvOTfV1D7/xgvbHiatPmyS5vnT918Z7nsedg9syftspdW49KghUAIoUtbhpwY6TAYyGBg9VnZyzHy6UJw68XHZO//6xfQVOJA4de6r2fAAAAABJRU5ErkJggg==",
    "maximize@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABtklEQVR42q2RP2gTcRTHP+93v1wxcRFsXTu4mEW5WPyzHBncA+UiuYwWHAUHJyWIToLdBdcg5JbiJg7HWU2roCiUDsWhpQjSqCCaqMndPYemRYJDoX3j433f9/s+Dw5RAiqNc6un3cLUvVSHs1aOldJs8Lz97tLNoNxxo/X6sOmtLFqneCXVX30r7uZw9OfOk/cXPxoQNY4+AKmpZltGZAMjawDl6ekcACNriGyoZlsgNdmdVwDCSvdNo/KyfZCoobd8P/RebQPYce+kYL75fmxnwQ5meqMoqmd7giDoOMWd6cImpPw0p4ATAAZARJYgi5Kkmk4KAaKong1meqMkqaaCvEZ5xJFUUO64LT+2B5lt+bENyh33aJyb51cWw8rytT04/0037jcr3YXQ6z7cB6aqNXAC349tcWeuMLlgl/Zcwfdjq+gFRK//+6ovSv41SappAumk65h+BtDwXnwWsd/3xQKfCqY0H3rd7YJTKqV5/0P77eXHLT+2d5Nq2qx0F6wpnR1lP/quc/zGKBs8G4tV8mz1lhp+i5irIkypMgRY7/XGZ3FGhHkxdqSaP83z4W1QORTov74wtuaYS4y+AAAAAElFTkSuQmCC",
    "minimize-2@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACXElEQVR42qWTTUhUcRTFz//jvZfPmZxAi0AiSIpCMBtRAsNpJy5a9cbJEYTctHHhNohRImgl5KoWEaZl+GjTItyVJiOGU9iHCxe1MIkWmenMm4/3/v/bIqRpUT7wLO/id8853AuEEjHHIQEQ251kQBz7lePMCADoP5dNp+LzHQAg93ICMEo1Lx6xbPuOrwq3/NyXVddNqnR8YShmnxzf9NZuA1ji/9/scgAQht9cazT0MtApF0nV17owHLWOjW95nyc3IuUbGRCXYaJwIYNy8FNBqdW++KvBWM2JsZ3S+vRUrn0AAOZACAXyNQnOPAEhR0xuOzul9XuTy+3XquOHap4ITBNpKWzH18UPQVCaHux4n+htWTwOMAKIyepiMxkwjP6ZfP2U4wAU57xsCpv72oNgRrNlRV8asg5Cbr8D0JLBCGNhHHV1vZCNBaNHMqOgVECMMyVRa/vkbU7lzi8BxNhuxnR87qgpooc95eUlgwRMAP7BptrK29G5i8Fey6TjgLsuFMG4axkNlwg/EGgPRH7FEnXm2vbWZQBPu7ufW9Fovgro4IwLGgXTACBdFxoAdKCuF/3vz4LA2yFGQ6aIdBI0MaIYABSLNWp2tkf980R+tw48WbnwceL12ftaqwOmiHQGypsPdBmMcQp1a9W/0xfPDh+KNE34qvSAUEkJZmpiXIcCOZgRrptUqfj80EGrcSxf2nj8KNdxlWDUW0ZMADBCgVwkVbp1ob++5vR4vvxt6uFyW38GGQ4mC5VgK0uMVgAgkUjs7exKPNs+0Pbm5t9fv2/tQogRiIWF/gJCBAWBJtzfZwAAAABJRU5ErkJggg==",
    "minimize-2@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB20lEQVR42pWSTWsTURSG33Pmo0mDNGl17RejEIqLSYNfaO0fcBc1acSdG12ouBTG/AB1q91I20HU/gGXVppYIQOC3bhrhQoqsTTBZJKZucdFbRIqSfWs7uW8zz3nvOcC/xEOhPvv+n5Acao8qdpq0xoPG6VlCgv2isOkpV3v7FUewBAA5NJrpghVYfKd0vJMmM+8u5eMH3+oRPld0QBYAGA2U64J8AQq2kglTi7UW19eLnrZvAPh/WG78plIIybtmIg8XfSyt3ckQn1tC/U6cQgAORAGiRkzklakgh8h2h+L9ur969kPEwBJn2EkvXNJAcD64bcjEPN7J2wcACmdwXNgCkTCNwBq3crT0073oVxOtJuZqjG/MePX6tsXE7FDlh6OWtAOJrVEc8ytnl/rGlacKk9CjPcKYZsgP5kMKBVsdUy6tLR6rjVojToAtHX6agbyiIlFqejGiDF2tBnUfqGOOAD/jx+yd0TqmUWSt1dmDY7PKwTfRCJTOurEi08XtnbzeytzDq81gKSQqdwdH7VcJcGzKOo8AHgiMH017PfxEq5EBbviJGNHHm+31l+53plbzEYKgKTQCobCOyNQuuFvPne909ccCCsYCxGHp+a8y82/1zg86F+FvwGGPMNeq2tCjwAAAABJRU5ErkJggg==",
    "minus@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAAn0lEQVR42u1SMQoCMRCcSXJ6hZXFYaEfsLDyK/EBfso/mE/4AZ9hc2JjJcHLWAkBIyIoItx0u7M7O8su0OP/wDzwfmsBj7bd8VVj0xwVwioB0DcciX4eqqqebpwZjLsUL6IMwdI0Gdgq4cqI4Trsl2dAcHexSRzxVGsGcAFIAE1JhUKCoUHiobad+/RmD675Zi2LV8tyesIh49Q//Y9wA0weLVs9jl7KAAAAAElFTkSuQmCC",
    "minus@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAvElEQVR42uWQPQrCQBSE52U3C2osRQ9hIaYQDxKw9Qba2YXcwBvYCjmIWKxYeAjFzn+T3TyLgIqCNmnEqaaYjw8G+L/QozKFIQjRh3UIRBEYIC7CzAQQ9zqzeom9Rpruj5aJXoeCmF23WjnTYT2ddzcAk/R9LbVG6phspJQ3MNkFisSbJWMLJTxck90YwND3tbyb+61FzSpqJuZEMnc9oRYGgJJlFgmvJsv2NueKejsIYucbEMdBVsjbP5obD59B+b2LI9IAAAAASUVORK5CYII=",
    "minus-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAAcUlEQVR42u2RsQ2AMAwE7yEsQM8KDMVSTMgCVFQ8jZFSJFIKKJC4zvHn/bLh53soL2z3pfcKBk5Jfj6RbQEDsAIjcABdTC0lGeLvAuwAKUxu0wmYQ9xVhp/R24D07uazdE3aXK+SQJJrpnfv8Wv9tHMBM94pNERBoXQAAAAASUVORK5CYII=",
    "minus-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAZklEQVR42uWQOwqDUBBFzxWsrCUuInt1cy5CSZ3Wk+YJNn5Am+CpZmCG+4HnkWVQs953MInXldUkUX0BHfDdcCDQAGOSSQ1qXWz3nqMv9/VauQXeB7kFhiSf0tF9bVcnfuZb2v5TflivVfnbaScrAAAAAElFTkSuQmCC",
    "minus-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAAj0lEQVR42u2RvQnCUBSFv/NeIthaBBsXsHCbLJClHCJTOIBjCJJGEAQx+o6NgkI0lYqQr7twfi73wsD/oeexjCXQ0KjPuKIw1AnwRzbSnDKn8DI6Ti5KR3AAdbU5QJ6wxq2q9a7eA2T3sBNTjbydEVjI2FbobBbJVpDYHM5t9u3j92r1zqjbJ16F+kEz8Auur1ooMvZzuWoAAAAASUVORK5CYII=",
    "minus-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAn0lEQVR42uWQMQrCUBBE38aNCtZqDmEh/LMEPIZ2duINPIaQswQscoiotUX8yV87wYikMI041TTDPB78X+S176R7sjfAenkWwBaz1dxME6O6mTVvBCIDE0YTkbosLsczIOpwmpP7YM12qON1HTyCtsgEI6CRcq+rA7BxOH0+L5N06i1eGF4+Y8YWiy9OZXZt+frSdkoadQ0ystCL7R/NA4ZBNFmn1gkAAAAAAElFTkSuQmCC",
    "monitor@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACIUlEQVR42q2TwUuUURTFz7nvfc58M2YhYxBuoqJNFMRY2CZ1Y21aFAyC9gf0X4jr/oPAwGhVi1q5atFHNNnCoI0rCyTE0GzhNDN+vu/d20IUNzkGHnibB/f3Lue8A5ySCACN0WaaxvSBal4GFIbIf484EDCFq3iIsf/PwkI2scvGaDNNAt5W+85PFrEDUE62ghkSV0U7/7k4uFN75JPobqV+YLKzt/HEIX1n0nFUiccyRB1VYgg7d8ul2vPts7/ve1UtKYPBkq8vvox8+x9fZq4v5YXvKBmHvdAMIIlYmYXJRn3ZXViuxx4MAaDf+aEClIRwhT9cl6JzoDYuveKz5RE9jjILwxyoj11T9+3SkzrbW6cPoqkAxs3NIQJ27FkBCBgZnQAAaebVSABq3nUAWpah6PX6ayDuA5ptg6kCiReRXCQRC+HazO2lFZ93xVWTw9RiO7ignuUzLI7euWoS89zuJPSC6NfYGG2mfYFv0mTwXjdsg+BBcwAACm0DCAI5d+RLwmCWJjV2934t7u0OPDzsWiWmU2rhsmoBg3G/iDQDGiQuwvDUCIOZEDQRD1LWWGm9XMgmdnumMV1vzk/XP/44UfsB49jYe3djfdgNDq/H1VZ5PvGVqRDbLWFS81JCXrS2E1dJC+1+vtKfT65sDcnm0JZm2XgEaP4g/CxDMQ7TudWrOn3z0yIAMQBqociLoCD7SBJmzblsojAYCRpOW38Bts0CptFf3MoAAAAASUVORK5CYII=",
    "monitor@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB3ElEQVR42qVTMWtTURg9333fzUvS9wIBGxDcBIfoogVB20EXFUQQpIOpm7g4C46hP8DFQaiDk7SgOBREcBTb2sEuQhyEDiIK2qXwXk3fffd+n0OSUqIBoWf5DgcO3+Hc7wJHAAFA59zaPY7qd0MoSEnMUB6DAkrKJp4qtXi8snVxiRZmNq9Exr4Vca8V4vTfzuEmUkORUeBmEH+NodL2sl8sb83e+N+4nZn1rwRznQF1AExn7n3ze+SzPE8pSTIdN+T5F0qSU9pqXVJsb1QAgEcxq0L+3dplDygBpBP60UFH6wAAc5S2D8yFZxpsxXAqddE1Iw50D2kAkapRJYJCuNZ3g7gkw6mLWDzgGPKBpkEBZgJ8ZGws+e/WwvnNXReY0nhfBknQcCHL0rgmWdE3lajUUDYSIj4GlZ9MplwVpfuGmtsIzsTkjXMWUIFq6FeoXnWOKQZDg0jMNeOl6HEUPSUAmG+/SOr1k1e9+qaKZGrKArDHLdknQVxXyH8isjGpSQ1xZiv9N8825jIaf5o7Zz/cYjt1xvm8FnPysPR7S5bTH6VkvecfL7w6dK5EIzLf7tlf0ztyIrePOEpve587BXYJaDAn1RD2lr8lxYPWzrR5+fl0OeEW/vo3E/EH9Y7hemFa55UAAAAASUVORK5CYII=",
    "pen-line@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACGElEQVR42q3UPWhTURQH8P+57zUfNAiRkkGocSyJEOiLpRJpIjgUR0vQmEJWu5ao4zPQpWoGF2fBBqWBbIIO4RmoTQWftIM1iFChFWmCFatN83wfxyEGi6VprJ7pXu69P879OsBfBINpb18Fi05b7pEgFSACOWmlmvD0HQ/umJu1nE4vASaAfovdkHj8uZRrI3f6XQGN2XrgEv1LKWXhIkCcTM5LohekUjlvpaPV2WOek9nmj0be4PqgZRtrgsUMAISKyW4Z/YG4B29st9bzc/po1jJaskv2DYD4HQCsAiR6RzbyBX00OxEphz2uE7oDe8d2eBpgCuEWH4BoMgCko9XZqdgGp5WluwAwESmHMyPLjcyZlXpK0YbaN6eKoyEjK/VLp58OAUAyOS/9M9KZuy86+n9Briovbk/FPh4VYQkAUsML16/F1nlyeDG/D1EOQQAQAGTiax7r++YXgEsF/Wz6SqQcdrsHNDDQNBuJ4vKF1UxQ83w4BWvv4kAgwaEiOAdyqL2lxZhX9ld2ra1pZvrsEt77EML4Zn8aK+njtV5+owwARIj3ST7JdJr3ZOGGabfemMbW5dLKeG1Sqd6UhHfMtJtfQSwRiBlMYLLdss/fsrbnHr0+V5Db5cF+vGs2gsz2WxPGs4evojUArEIV75n9BIoCcAgktc+DALDNTATmJ11TPeDFHl6w1Lgmq3FN/lWsqFewM/4TkSoyUA5q2OsAAAAASUVORK5CYII=",
    "pen-line@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB/0lEQVR42oXTTWsTQRgH8P8zM5vNdvOCB1tCfb3UGopooqJVSAQhF0UUt6L9CIL4BXQNnvwSIlZUiBcRSlHbJMWKSGrw4EFQpCKKxDaKdHeT3dnxsEaQavIcB37DM8/8H2BA2VBs46kiABD9oGUpXq6QPLfnyVhM33QFCjt9+fPy/SY1bChG/4OFQlXU68eCM/m58RTPPAvCjgGgRgr5AGzi3nJ+lfWFE3PjSZ5ZBIi4vz4qpXtTE4kRCjsmQBvf8xccyiySAvecD9muoMlUfPuDjv/94d3mkRUbivGB0G1lmUjmkvGts56/tgBft05/HZFlFJUYDPVcwhid9fz2given6g0ptxo2r/btqFYbzh94YspN/o6UgAgbChWBoXW3qdZkw1XoVRfWAaFvW55HddgHSoZBswlEKWd9U+7hWZGMGjXvnTfnMwtn/dQKPIdK7dUETUqAlRHPUrKdH7pbIynKy35enMC2/alYlseu/7q/Exj//F+ISIAuJB7XmEkSgrythkbvuj5P+bDbusUxRJjITQnDJwQ0MFJKSYMPQzczp3m5DsR3aC+aXxIBNI56gbtq57z6IZmlHbFefqVVF1AS0eJRghdJOFItwHggAAAk/RLgehcn3l58POfXGettxRfO8zJMAJ4AAQISknpEqfw4z82yGaFQlX0tmZQ/QJZ3gPq8o+uMAAAAABJRU5ErkJggg==",
    "pencil@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACgUlEQVR42o3STUhUURQH8P+9b2YcxXIRpJsSspUG0dw3CjPQMETQJlo9sHdfSQU1E2gF1qbFcypCsUAiMlq00aQcCAK3Ms04n34FGtXSZjSMwKKYms93W0yj+T1ney4//ueeQ1BxCaIDxAdiaHLkiERrGjPG17lX06dSgCC0UkRRQH0gBmeR+2a6Z56AjllJ/SeVhc8ARJBKEJfrrRQMugtnWbS/rupA96/M4tMCsiMWWjsgUGgSFAcrQAImAOC2SJ/HkRTcFhksd9VjoXMXWucEbw017zQaURTQYNBd4CzWb7Xsu5XOLadNUvXJ9qPjLQAgSVWnC0ZG5ERxZTuI6K6A5PeTIpdjfXXWxu4/+ZW7eZJtA+h+k3nvGLdFBmssDUpR5O75p9zLWyqX2bQZAFRbWPc6lwS3RXvLPaX5zWFNnkx7nYtCtUUfAoCijEqbEL38Jyzc6XGkBGeJxyXgvQUANBa/c6nto+As/mQN2bC0MqKyUJfHkRRcnhwGBCkn5HK896rzi+As+qBiRLNPDQGArgtaSpIY8DqXBJdjfQBQ2uYGpDyjyiLrEZQQLk8OexxJwVnsdum92JxEQQlptwWvX3F83oRo9qmhEhLuXEuCrQ9ZlScOXWz7IDQWfw2AbERUFur6b5tbIhQAgUFOSNSCgpHrASB86IHGEkO15nrtZ3bh2sjM8Ue6K2B6NiPnAYhtT1hl0XHOogsA0GFPNHAWf+51plbHKS9ipyId9kRD3iguEIikEOSbWap2mGgVMvkfN17MOgZ0V8DkC7oLu0Em5AwrJJGm1NwoiPG9KPI3s8Xf4y9nne8UZVTy+XdHVqvdPtFyns03rburfx9eaf0FvyYmAPuqxIwAAAAASUVORK5CYII=",
    "pencil@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB40lEQVR42o3Tz2sTURAH8O+8/dm1raHUahBB0UNaq7QEFUG7BqVGvAkbLdr6Tyh4EELxpAdB8B/wIK2NBryIvzBu7KEFQ61UjRc91qJG0Dbs7tv3npfmpNv0nQaGDzPMzAPaPM+b0dZDdik7NzGenb+c3/fEAgDaCLpuRff9XHx++Hm/pfc+gJIHNGaCi6Dq4NsZvR0cy1YyFut5oSBVjCAT83C7bab8tbjXY+0q6tTp20Zqp5Dhl/tvj33mWrAiJMd/227Bc4NPM91OelbIMOQiuGMbW29GYu2NRuZepUSkdfb1syTY5aSrUCQ4D0enF0ZuBdHPK93WruOANJUIz97z9wSUDIEm/36i9O7Ux8LBZ4OOnX6soIxINEanarm6hxmNtdaRBL2hlwMd1o5XSslUwFdOT9Vyddet6CUUBBWh2CRIjmUrGVPr+Qc6xrbXIOBPc3mkvJSvtzpcH5iiC0fm+izZsQgi1ow2BwGAAaRYLK8RsdRq+CO3WQgArIgig8JEyH/dfbR48sPFoeqAY7aHAEDjh+aPQumzQobXiVSXRvZVRnrjt1x2y7VkCABMyni3xkzGiG4QGQVA3W7EX4fLtXy9tYWkE9a3YOnhKt8vJbRP0wuH37cSRSg2WSKx0cf5Czg8IG/ATEmXAAAAAElFTkSuQmCC",
    "play@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACTElEQVR42q1UT0gUURz+fm9m9o9GeDDBKNaga9S6i6EVq0XQIQiqMdhWqEtSoBEEnWqaOhYdvHUIknQPO5TXgsSGMiX/opegCA0PtZUeWndndmbe67CM+WfdUPpOj8f7fe99v+/3PaACEokhGRCE7aAjNnbmUtNM54W4udffU9WM9K868g8aRruXbBzuqQrUdrncAhcOOMTtn0vvH7z8fN3WIBgA6CC+CZEggETq5HS1WMznBMQAuHWDWPhWSK65WvTyC5wXu/snWwZKlwrJMMABEquJmL8ILOcYgUBcfExPnZjvn2i5VrAWowA+VQXrXnTEx14nY28OGAZ5AIlS//5CXv9EQcQ1aGwu0RroNY9OAzieio+cIyg9IXnXTEf8Q49nZ++mzbYlvwIgwcrp1aHzBsDVIJgGwfrGm59b377sd3juDiOlWw7Wz6caR6+oyEgliYJYJSd0ENdBXFWFZCy0F56NHb7vuPkGDu9VVbDucTi+7+3FptGda3pUCaW+CEokhuT01JH5vvEmtVD8foyxYDN3vcubStsKiJgo2+xyKFlOnmnCTUaHI0xSHoaV2vOW82uEJHoKCKpItDKEBnnqnkw4VB+5yRC4JwT/nbeznfbk1ycG2r3yRAKkQWNzgNwLsgDAt19h4d2Ot1yyf/b0GvvljZrJ1qFzmLqVPPjukKSEHoWUmjbLWRq03B+n0hOts36gTbPN9Sd8hahYvYPDLgCEhmR0MLI6Ink7e3Z9REyT3IqhrQ7UdTk8v+XQbkAyNrqtbwT/62P7A2VJIde7nF2UAAAAAElFTkSuQmCC",
    "play@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACGElEQVR42qWTT2gTQRTGvzezSTdaaRFBvPgHSoUqgm6KVsFtPCh6EFLYqrG2XvRQ7MG7si4eBC8i8aQXLTH+GVAQ/+BB0xVsRZsiHoTWShUUPBSxJDRJ3ZnxsKYmIkHxXeYw7xve9/veAA3KcW7zRvcUHppce4RPF2JuhMd2B6p8PZPfmgagXWgGnIEHT/0uZradMwDS74rmYNRYcipQ85+J6OJA58T7VHxsrwdSHjwV9mmqE68vLqNwBLVxISh8yeS7eqQsrFVavzZ5y8P+eP7+gU3P2n0/EQCkHUcvWjEWn1F6FowMx9E8K+gjgJ6DW57vijIzHTNbJo/EX54vz384KwQVwwlIs1/uiQOAECRduMy2c8bNiR1Ph8etDYEsDTEyBmNL22YOW6MDAGnAZexPFD14yvcTMqStKZPfdmlBfVoHLe+akeVXU9boScBTrFEUQvRKABhqm2q6kd8/OzzeebwSfHsM6GMAwP4m5/R0e+WQdW9Ff/zV5SajdQ9AV+qB1ZQLl43Y3UyIRAAAfdaLE5xFz2mgXP7+9Wg2v/0a4LIasdYAwXE09wRJ+J6q0mbM6JCqEtJ+21ulrWrEFIWGFIJkavOTNZy3XojwWDKQpQel8lzy1pudU6EVzYUgCQDGquaC/hnVZJQ3r+yzxu4Q8SQjminLuX3Z8a5HAGDbOcP3u2VVWLfbToeIRM3Vp/9lt/E/v+oHjavmDf2JXUMAAAAASUVORK5CYII=",
    "play-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABBElEQVR42q2UP0oDURCHf7+XgIoo2NnlBIIHMLWliHfQ0gtEK7WPhaXYCymtxCYQryEEL5A64bOZDc9l3V3MTvP+zMz3mJk3I9UI0AesFuIK5zNJh5LebM/jrmd71UgDerE+8ltGwFboEpDqII51N5wnwAB4ivMcOM8frQw3A+2F40OmOwY+4v4dOMrz1wS6izC2M5sL4Dv0Y+Cg7F8Jyiq2zguwA9yEzQK4zHLrWlC5GLEfAK9hOwP2W4OKV0vwk7C/lqSkzYXWoPiQ2F4WoUmaSvqU9AI4NQASkGyvimRL+pJ0KulK0tD24q+q3XdV/ttNP2TRIs//apGumrbbMdLFYPsBOyjV9R/cOQUAAAAASUVORK5CYII=",
    "play-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAy0lEQVR42q3TIU5EQQwG4M7bBxgkHsdBMIRTIDFkE85AQljDAeAOKARX4AAgsZCsRKE+BJ2kIbvAy1LTybT/3/nbTsQPhln8ZmgYcYFHzNEyNmBYBxzTz33ZXfoXHNe8TljBW+lv8Jrn/UJyj4OVUgr4EssaxCGekmSB3S7zO3iBZdE5FpIzvCf5Sc9ZCS5NnJXG7aU0OF9buT4tSXbK/QOeIyKGv8y5tfbRK0fEUUTc/qvmq026fY23qXPuG3Y6ecNKR7cn7/Ymv+oTHr+GBOiAUqcAAAAASUVORK5CYII=",
    "plus@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABUElEQVR42s2SPUoDURzEf7O7+RAXBMEYMLYpLGzWQqvgITZFrPQiHkMQKwsFt7Gz1SZWXkAhVkFMEESiaEj2b+GGhGCSTaVTvjfMm3kzkA7iP0GHmDPLmTPpIuTCBagF9ZOH4PYKwDDmFmpVVgQgUQTb/rEjm+TKmxnM9Ap8zNVGGF64ENJqXSt5pFfq5M4MKk3/aw2g07mX75etUGhbFFVjmJJ3FHtB/bQW1J9SOjKFG1Emky8deU52uR93PxEuEBvsgK0KXSZcHDwvpqcuuYPobusNDG8gVuz6esnbOmgTzAwlRdgSKGOwCyDDcCRiNfNu30uTTEm081pQf55FdsYrGn78xBFqhKsJQsOdNBrHDiCTYmGDcw3FZKON/TZIA/D9sgEmswVQNjm3UU6qZRdu2gNyX+hxPHrqZUdUY4BcLt7vvy+6447/EqZpkQb4BtLbcPh+Mc87AAAAAElFTkSuQmCC",
    "plus@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABmUlEQVR42r1TTUtVURRde99zz9XnDRGMTCLiDUVCvIpJQZP+woEgChr1A3Lg7P6A+g9hOAjvP3DiQNSEXj2kQRm8aCJ9USDh6917zt4NLsXLfDWyNdyszWKttTcwEEr59Q0DKOE0QCcpAqRufmNiCOl97ZUPVveufvw572fy8VWHggHASjLZMONLZOkyADhX/MHl3zzmylNwlEOZJDnsVl+8hEhzKI91mpznyv0ZDAzDTW1ebIyMvIOEayut+a0BnmsvNxd2zg1rOuG/H3U9iyr8JRs31lWrO97rUzaBLY8Odenb+ye7ix8AJc6ylgEA9rJso7QNy69tnOzbuLEepAci89jGyT4jeWWjtM1elgEgy1rml/Ldmedng6XpUJVMpBpELsTGrnitlljphbCw4eEQlfryUXv2E6A00POt7Nl5w9FBkHBltTW3exLH9KftXMFjnSZ/bXZE3lSpEnkK5Rnn1iIAEYBQFE6O993X81pUK2/P3lt8q7fntm/UPdfzvx5JAScAUHLv4Mh/fqil7gFArfh/8O+v+gHkSq3MRHONzwAAAABJRU5ErkJggg==",
    "plus-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAA8klEQVR42s2UsU6CQRCEvznAhMJAbLDxFaix9FV8KRt6Wjofh4qCzlgYCvjHwr3kPPL/h1ZsdZed292ZnRxcEbbFzYRt2U6tydJAgVEc18B7q2EayOXuj8AKQJL7pkpXsPsAvlqgcQ8dAWPbABMg2c5Y+SdhoJPki0KSzsX1FMWP8eBES4fgPQHegAfgCIyADngGFsC2YiLgFfgkxldR9AlYxthZv1k0eckKBHZfMxp0s+2N7cOf1l+tNlU2qBuoxNdi27ZiE8l2Fzr98k+B6fdRAXCcp8CdpHynLtIyZAafgd2/f4FCi3vb89v5Turt9MU37ueDmXhSofUAAAAASUVORK5CYII=",
    "plus-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA3ElEQVR42rWTO0oEQRRFz+12wMAJ1REMZGIjEzfgktyFwazBFZm4CD+ZiTLYfQzmtbQD7QeZC0VR972qA7eqwoTUAC3QJZGdq4ioC/VGPRr7P21ua75wo6uxP9beFjFA1AZ4Ad43JRugUQUcMvjcXIZAX4etq/6WpB/8L2Q1SVSPgQXwWoecVc9SfQIaYB94SPKoBnVWpJW/06r6ZxmRD4HzIgicArfANXBXfgfcJ3n+Nn31pEiXUz3baTc1euCg0p7XNbVF7qfS7lSSdOp8qNeaJN3uXthflP/8qg/WFMw2T/fGXAAAAABJRU5ErkJggg==",
    "plus-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABMUlEQVR42s2UMU7DQBBF36ydiEiWkJBi0nABI1FDZXEH9gBwEY5BQ0VDkYaOmgPQUoYKAaEAoYBQEvtTxERWhFlTwTQrjXZm//z/d6BdGP8pDI5dCJlrrvcRQNY/OM3Sm8vQa42NcsZWYRgAu1VaTahccC6zF4P30L14dRwPjBdo4pycJ9FBuJw8BpgwsYREV6SCYVmhDEeW+rPt1N+39Ydl+A6pTiJFG4WVHwaRoDTYQ2zKuKjxEZfIejM7un4evn6NZgBTBtbVwxaOHROSlvytY+og26/4lnBmxt3bfBa3dnOW+vOs7x9/K/9SWo9vMqHVTmtqtPTJiJFbqK+ylq83UV2x73wkgIREgAQ9oLtSqNaGXPgEEIWZbkN/7QfWhyVAUUwP1+YWrSL+6zUSXmyfirZafgawKk0AAAAASUVORK5CYII=",
    "plus-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABfklEQVR42r2TzWqTQRSGn3NmviRguij0JxYXIghaaxGycdcbcNPCgJdRoQt3oXfgLehKGMTrcBFUiLWrLqWpi0IhtGa+meMiCUZMcKXv8p15OOe8cwaWS/bY84DwL+QWVQTYXg+dzfZur9O+/3k4Oh0t6kD/ZIMCFKu3Ktc8SlbtAoSpvwwW6ClsC/RUxV+lclNDNujpGas6Of/VgZ+DDY4NKADUz8bSaHs1dwPHpT/z5zRL03Y2nm+a+U62dC0kK+ruTgKxew/XDi68OS3abInU54OLt0NAfJeu79NPxfLLhm8dSkkIDSZzZ0TdG4/DMCr1jOsfr4AXXbpeZpWfdMJ6smqnWFaRYlLsjqh7XSwfIfLRmWgRnytJg0/n8TsgSxfgwdr+7co1v9Wkp1+H7z4sujMfmASCRlY1cFkG5LZhNeZWAsF9AfcIciSWSbi/P5VFYobLEonZsJVKWx4gEvMUzDNwyYadALBx67GZFbyM3w9Hp6OTqf9f9Ndf9RPds5ZG0Vyn3AAAAABJRU5ErkJggg==",
    "printer@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADN0lEQVR42pVUz2tcVRT+zrn3vXkzOEmaTBIVF1aKpWpsm9jWGNtBXCjuR2qsVCkEF/o3CK5quxDEjd2oRZvCE9fdSEga26CdCYWqTQvBgoSmDWnGZH69d+85LpJMIiYLv9XlHO53ON93ziG0oQSQnj4422WCqL/hVsQGTEBrI5+BS0Wztot9en/x25uvrWz+AQDaIgFOvjDTZzOYtCa7X1W20lvFQGThfG3OtWrFy7def7AeJ7UAUCrFHMdve46uDkS2f389WfyMleeEhVlY1ilABKiSFqKgcM5D3wDoYrE4YScn4ez2eqRwql4AxN/NjpSxA8aGbhQSTc9CyGyPbxIZAH7jzQzuLJXUAL8Z4HkPAHvmwY+egTT+vNptkWFmFQDIZhsGgLOAUhxTsiGVh8IRSRLH5LeRA4BHGTh9cCJBCKcbLV+58lYLUCIAGB2cfN9y/hMnze4o6OpouUdrCqpvd6XdPhBkbOeeVvp3w5jMYurWPh2fPfE1nTp8bR/ZzO8kcssj+UNUPROHABi7QaUJImYKDgD8nPpkwDqLfTmKggaWPhwvF3/B/8DJw9NPZuxj856TojXQVOGVOcoDSqeGr/Xm0NPdcFURTduDJEpk2YrRXKekqwsXbw4vGPNzHtCMAMG6a8Sk6tLRgekuTexMat1eS1mAcv8ZSGMyaOravTMvXx9suDAFSJlUrACWQMqOFsjax6Owd28tuX8WauaIhTfdIRVWYgGqhVzYe36ttXz0LvNPLzLDKawlb5bARGA+I+TvekkFqj9cqgzvOJAfHJrqddI8x4RjB+CHiSxBTIUA0Ojg9BfZoOcjJ00QCE2pja5Uqz/azj7jqg88AOQLXby6tCI9HR1HmYMpphCByaGePPzyUuXVj9tivndk6lmV4M3QdHzecssj31eOz+yyIk+02P7lfPO8kP9mvDxyu70iY0NfBRd+PXHnnSPTPYZDFuDYu0PXIzLC6tc1MiDyUK1J86ls0Mctv1q5XD5+e2zoRnCh/FLaPiOlUszhfH8fIZwIbX7XM8Jk0XTVO5a5WH/63sM4LglASjsdNkT2kJO6UfX/YiIyahGhbmqz8cwry9tX6B98kYGaTUCaxgAAAABJRU5ErkJggg==",
    "printer@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACb0lEQVR42o1STUhUURg93733vZnRGUVMZhVELSKohTMmVMZYYMt2gmhSRLWqRdmuhQktWuUiaJGoEDQEQpuWEs6Ejgn+7IR20SJIgsr5efPeu/f7WjgKzaqz+TbnO+dwOAQAgBBAMtm/foZMYti5kARMaIGgRKkEGI2PbzeHvhzyDYAjEit545EZsBSEJKSFRCBEIHFaqQRbtVMorAyWynAEQLW0BRCC8MnYVmdjNLJJH9kYjb7YRzZCPWvjxgNSqv90LdNJLb4Bpgl4eiBAa44YwdL2yB+0YSJfqUKc/PK+RQexQQaYYWAGE4Ofu2AdCyE1ml/u7vI69X5cd/A6NeK6I1FpAbuMOtUD0HcAQpPnN3odc1GTGtEqSSwWjsN2Y2jyoZUPy00w2+WoiTFjrb2e8NLXYledYra/4QAoUe3P7ELHiCEs3UanZpEIRo2CpCNbbxa3h17gPzGeW3tMoHMGRI4I3u2Lq5lmE498nbkbuVqSiI7cRYSNTvnMwcsw6J4B7XsgOAUFTwTxQmWoKqA7jHgPRHMA5iEyx+B5EM2xuIqIPFzaPRuRSAjAMxC9l/QyyfHc6rRWfkfE4ft3W5eetUcd6/90w9PJq+O5ynPfpI9HrrZhoiD4IClZIDJTnk6lObKmUFgxJwDzFbCHl6q616gOzVK7H7raYhr+0tE07+U3vTriPWH7qrhz+QkBkH9KWr+llVlMCfW93hr4idauqVBY0eXyFTuRW9vQKpWzEvwggZbD4ROcVslj1oW7xe0L+UKhpMvlYWcAyHC5xGUIMVVuGqIxLV6PQ0yAgoChREOYq4Z4ASAplcURSP4Cri8zpimYhZ8AAAAASUVORK5CYII=",
    "printer-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABsklEQVR42pWUvWoVYRCGn/FsjAohQRMSxUYQ/yAgCokWgp3egp0hvdcRtcwdWHsL9jbGRjTRQmyEiARFkJjknMfmXViXHKMDH7M73zsz787OTBFRq6pUZ4B5YAQUf4rAMWC7qr61PnSD5Myrmx4tm8GWWgBNYh2rqqG6CFwGHgNbyT4KpsJoFngC3KuqZ2oDHLSMBtF31aF6kzGizgazkvemy2gADFt2wHSC9+0j4HSP6QA4aFKwvRiHoblXVcNOkPYOdS+YEUBV/WrrhPpQ/ah+TzF/qNvql+ju2QnmZ3xWAEq9CLwF3gDvkvl46I+T3dxfBa4Bi6j3k2GJ/xT1nLqrrjbAfn7rVL51LgXtN2TFNg18zpkCJoGJpgPaB2aAl8CFI8h8Am50SIyatIDJsJAga4c0ZPs8CzwFloAXuWsa4GsYrQIfAn5eVa/G1GUunb0M3I7vRjtn671ZeqBOqqeiJ9WT0Xd62PV+pkvqo7T/rb/8qbPBrKlXWnuTy4mqeq+eSS2W1RNjhvZ87BtVtRnf/e4aGSTbv6yRLXUhPkW3T3qL7XqG8bDFBvC6qna6i+03F46281VJHJQAAAAASUVORK5CYII=",
    "printer-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABYklEQVR42pWTvWpVQRSFv3VzrqhEL4l5B7FUX8DK0k5QELSwVEQsFWITsDJNWvMeEXwAESwFCzsJROLvFRKTnPtZuAcOVwK6YFjDzJ61FzNrAqAmieoF4AqQGg0Wv0ryvtWjRk2JvPEP9tVD9aB4v9bfql2rZ3Aw6mf1uTpRl+f4XglMWn0HRKVs98Beku/MQZ2W/YNqmC7JrDbPAjPgVKkvAP2AF4uXkmwDop5Tt9SZ/46X6nIHXAOuAo+Ab+VyxN/oiyfAOnAd9b66x39A/ahujEpxrJ5RV2tjV/0yGLvqD3VNPQGMgb6ryWGSqXoX+ARsAV1d4Ag4Ai4CD5M8Vn8BY9SbdQmr9c5PjrF6q0LzrOpvoy6qL8rWTH1aKTo5xw/UXv2pbqqnh8pj9au6dkznO9Vxpa21XC8kOVJfA5eAnQrH8JlWgHfA5RaclutRRe88cANYmvtVAFNgM8mH9qt+A6mmj3GaiWYTAAAAAElFTkSuQmCC",
    "printer-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADUUlEQVR42pVUTWucZRQ99z7P+77zmfkwrVZEWqgGhaK4aBEXWeofSBaWkoiUuFBEFI2kNJnW1HzZgnUTKMS0UiGjLtyL6KoI1o3RpoJFKZH6kZnmzcy8n/e6SCYZIRvP8l7O5dz7nOcQ9kAA9NLkUtkrVx70/ZYYm1BPH2litVjMc9hs3Huj9lKzy+mSoapERJievn7QVfnGdTMDKrLT7ZmlCjYGYdhZi6gzODFx+k9VBRGpBYB6vc4AUpb4WLFYHWj6zVlA1wzAKSAAQKqkREoR9fcVS3O+T88DuDo1NWUBJBb/AScqIqpcf/fsye+xDxYWvuxPkmRGBKa3bgFgdRUGQIrthRlEpZWVFQPs1RuNBlcqFfl9tVVFwWNjRABgY6NquoqoVhuOAEAYKaAJQaPh4eG0O6R7awB4f/LTyIMmAhIAuHz59RAA0XZzaTSby09GUVTN5wp9W+3NLYDaUCUQae8KpHBy+UKl3d7quK53L+i0z41PjS7RBxeuHiVyf1LVH5M4/lkJKQMuQIx9oRAgAIgda54g4ic1iI7ZNMXRXM5ztjbvvzJeG/0O/wOzk1ce9nKlX2PLg5bZiUVErWuKqkoLC9cO9FG56kctSZM9QyZpQp6Xkazjlhqhvz4xMbI+d/6TIgBPJHUsABARqVA8M3O9bFK+EWfSI67jghx3z5AEqCqM48INzW8Xp64842RyMQEKIrFQsUysbGSdSB8qFfuPNO7/M8PMawplku3XUaRMbKTdbvWXSuV539fjpTtbX4WP9YOZrIXEfwMgEX6ZEv0l9RJRmM/ePrO/IaenVw5EUTQHxQn/0fyzWWOIgZsEgGbPLX9YLJRfjeIQRIQgDF7M9G18kclkTBAEKQAkySG29g8JmtXjxppvrbXwvCw2Nzc+eufsyGu7x1yYXH5cHfNCLlu45Hf858bPnLqxn6KL731+yDhyN07ieUA/fmvi5K3dL7K4uOiMjY3cvlBbfsBayyrpifnz1zKAMnYcLEzEohpK+5GKV+FOO7g5Xjt1a5s7Fu/GSH24znefCg+moK+zmfyAqnSN35MiCmMsWi3/Njx38PBh/DU0NCREpLRfsNlC+ekwCoxIRDuiASRgZrU2g7Td/OHN2umN3mD7F97Zj1za3SknAAAAAElFTkSuQmCC",
    "printer-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACaUlEQVR42o2SOWgVYRSFz/m3mZfFEFRiI4idhYUL1iJoaadYGLRQbLRQYyK4zMxT4jOCIoIQ4voCNoKNpYidYGUn2FkJRsElOvv/X4ssSCq/5jaHey6XjwAgIiQpve7TbZHr7G3biiIkliFFrI1QVdXrqavjH1fyBsBqSFH1tda76xoVGTRAAYQh0JOMQHmfJG/2APAAoJY2U0SEIrK1LPM74vIx3cZj4Xe+UbfVmLh8rCiKM1rpHZ3O18GVvAESiqQgKTPdvoeE4uLFUz+xhl46vxhCkC8xahFhmoIGyAKZ4W4yv66CBAg6vanZERuP6qb87m2sdFMGrzSHgOA3tW6U5GcAwunpp+tt0M9Itd9ZRx88mqZeWwxjLKyxqJsa3vtXui2OGOt5MIrjA2VZnC/K4gcUAFn6xb/4tva+reGDjMRx504NOWQEHCrLopy8Mn4b/8nNrD8BcruRAE8l9uaFB8McdueiaOBkVRUxIP+0MzjnXN1W9/40cQZWloBXIKwIm6lbJxYBnPC+XRDFOQEfCmROIA9Fcc6H8FYCzmbZ4RqQSgBrQCwMDgzHvW4/McYONE35Yio5fn3tqTe6T446E+2buTbfi+OBzVWZvzN5+/Mlcz4ySp93Lh7yvjVJkhhgiwE+tStTQa+PXKzLMj9dVvnjkXbk+aqas7Oz9tfC4IKIvz959dilJWtltXmm2z9ujH0c3ODGiYmD37DsNZMk0VmWtTPd/jsbRTubqv4ihIYIQIICb53bUDfVh8nL47vSNNVpmnqzvD6ICKfTJ8dswBGl1aj3noACJIBKIQRZJOyjZa89SfkLT/JB7ahkwa4AAAAASUVORK5CYII=",
    "printer-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADAUlEQVR42pWUz2tdVRDHPzPn3PsS86tNXtJWitBSfGnaaDHQIi6yVBAUlUfVbgShuNC/QXBV24UgrrpRXAm3RN25EYmrIqQiiWnTSqFSI01CbG3SvHfvOWdcND9etVn43Rz4MjNnmO93RtiBAPbswKt7Uk/vvlBuJCMTHgmozOfdqutrd3659+3drZyt5O33+MibI0acVvENdoEgRAsLgk7OLX21tEmbB2jS1IIiCuW4155GFTY+NmFBTNTEEoCZiIiZIHXvus6HuPEi8OUkk36a6eA7f0tRgqolRYrZ5WLmcR1NHHirXqV0zsB18h7g14dk3OQ0GgNNmo4O/iZ79TB/pdkqDeZOFSQB/EG3A4IHZJ6iBFAhGgSUsqCIHcUB4gzQcKdLgyCQAH7juzYgAjA2/Po7KrUPjTDotNYfU3vNjAedqnRMO/Na2xtje0PE30lWfTS/fOlzGRtuHhHRecHmzOyqCRHIBXQ35QxaYiiiRwXGkqVxDxxxkmWBtffml775if+BxtArT+au56ZpmvQmWoGZmesD5Jl9rw0n7R8MVStllmSniyhBsuS8HyjLcvHGarGYU+tDqGEp8zsuc9X4wMt7YsouO8Kh3G2JJpshRg4ICo5bYwebz1HGCrwJmnwy8yAmYovJ9e73rnYohPVzJrqghiZh05CoCMlo13P3xIWq3TqZP3Xr++p2g2TmvXhbwRAReTdpumGkpOil3Qx5Yn9zOFo4j9ip1u+Hn/dOBdErAsjRkeanudbeTxYBIVr1dtl/f2pg3bt7PSEC1Ft9utJ1P2V3e0469T+KKCqeMrU+u7pUfLA9zNH66ac96SX1+SdWPnhhdvXry4/raLR+5kDm0u1k8YK48MXcn1PXtldkgrPZzMrF68dHmkOK00rl1LGhN7qcV42k9LCEE4iWrDyo0q0hlleuLk1dm+BsNsPFquOMNHW0Hkac+h+c5g0wMPmPFUWUkNrXzZg8tsxyQZH4V+T2YQtdvSfUggOT8Oh+mAdiK/08/3ex2rlC/wDuB1xdchvLbgAAAABJRU5ErkJggg==",
    "printer-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACM0lEQVR42o2TMWiUeRDFfzP//5esJl5IYjZyIIgceG6wiIrtiqClCGY5QcFCPSzO4tTOQgQLq7O4PoKFFhsstBIR3E4LsThuRVtBcVWiRMgm3zczVyTrSSpf8wbmPd4U84Q1CBAzE7/tJulBMAlC+LaMUArw6vE/n9qvBvq8PgAQ2W9nSfstYgVIEAEiIIYw7BIvmjQPdHhiIOjAB4gEO43qZq1kmj7T/VKm6DNdK5k2Vi6Azr4Zr4+ABCDfJwdgAcvPF9tf2IDGtuNLRMTqyJtVFpGB2QF+mTj5E6y4uGzaOd4aqxmpn7ABEz4aYCPLO8bh6Vsg5Nefj01qVdxR9LBKksDxsI3BiChKwsPw8EcMcyJrmY+mVBwxLy9h/tkA1dCNZnczJxDxsaTFTVstWxl01Hy13+0t/MUPolFvXRZ0TxYwkGLX5NEtKQ1dVCnORVS14P90QVwlDYVXf/uUXOMDBYJlIoqQKF99ur/UmJo7C94DHipkR1wJDaiCmHXkz263faVRn1sJKHIovazDtUZ97qqSNht272Vv4frGU3fXj59SyYdm6nM3VIe2m5XPMsGDyst5QS+p5lEzz02aGchANeCe62RKOVn4H+7lrU15uZ27H9pfgTP7+P18v77YE42iQ6daNzLgRmp9cSotU97x+t3dj4NCSJNm6tCpZqZaz0TT3gh7v/bb32BK2upU3X97C/vW9ZaB6HDQoSPidjrQE4iM812rAEJYwtP8mv6JgcR/bPMCfFXaEEIAAAAASUVORK5CYII=",
    "redo-2@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAChUlEQVR42q1UTUtUYRR+3vd+zEcjKKEYQrUIChehXjUTZZISXGi7OwxqChUihFG/4HYhaFnRqha1acLm0ibDRZgzkmlQt7JIEqzcKOIic6zm6973tHCmRtOS8Fme877POec5H8A2YcDguk4SQAw7h/8gMwziANBdN9HWW/82Etx3x7uW4Zo9D/73qMQQj3MCMXLh88nFnXtLDw+3azf9JpjYSLYOuh6VDBibPghXx0+eafhAp2rt0Xbtob+wTLZeUOImmACAPm3e73g+S3lfapXk7KGmhDz7tMevlt7OuIlRH0kde+yhlIlLv0XT9ahkWSE3XBVvVZTAZRJODUAy5aIRAM44XOGmCAJ+Zbc36SyP3rMbjxsgLv/KxGJuuOZJaJdafj/tJOYIznUCS4MxIkHgjDNXuERMcAlKvyDHy8EerysHAHqPTO7vrZ/K9NTaQ3plVN1Kw+66F1fPNsxQV+3EhcL/8iPNlmBDOFnq9yqKksgsnremQ5m2A8OeZIXPPfitiC0nvayo+Ksnm1ZuFakV4ZXk3MXIq8ZrRjAmm2PMAQAEgzEZADprJqxObXwppwjLN8LI+bu08YFzTYsU1sYHCu1/zhFjAuCKrgvJANiavIA5dswFAFfgwZcfH9sG7aYbuh6VzLEWp5CogJWeB9Ty0OrsSL2J1sk+7aUyE4gREEdZGdGgxRYALOQbs1E7lh+o043PAtmM553EVJHKJDoGp5rfbzaslhVyN2sCy2+2CVPoVSOVPqUkJnNPWdpZsQnsEwiOKgdKstnvkcjro3e3IuMAYMIUBohbb05Mr4qlKlekrzDIWUZoZkALiKrBqSSX1/Zuzo5dGgMGN4IxeeNm/yvITzV8GgtqKr/yAAAAAElFTkSuQmCC",
    "redo-2@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACI0lEQVR42pWTv2sTYRjHn+e99+6S5gdSSHFw0EGwgyK5SiVVY6CDSos4nLW24mY2K/4DRxYnJzedhGrRBnRpcSrJGWjFNqktLkUbBcWi1kabXnKXu3sfh5BaJQX7bO/wfN/P98v3AfjPMYAY7HVGj70JXdVmj7YT2EWNUNPuywCEQt7qj6oHlofjhbEMoDCSOd52RdcnJQOMvwRvaAvyaM/rh+nERxqJF27tJMA/ngyWgYxooSphlTneL1S5R+s/AKIReTzIOy9a7rexiWLfPR0mJWxhAiANHZ9JqHL0jiBKEHlSUxwRAYHI3wBkUZXv4zV/I/5koW+R6zpJ2Sz6wz2zyTCP5W23suSDdxuJGgBEjHH0hbAAxZWw3DVgueszPtTKBhBDAwy2pg1KFnhLnClVVcyfelBMuzt9X9byqZAUm/aFPVfnzkD2VaIOQIhNj+YhOdBZdrzK4ETpzNT1ZC7wNVin3nrQX92U+qOhwy+q9ueXFSqdnyqma618GACAK0uMgIAJ2QEgrHXFRCSy5YF5VoBMK7bz/abNP5xrLtJ2sAhAqJ+cCyiu9I4xtjw+f+LC7pVpBtt6YTKZ46aZ8kbihUuRwMFnVmNtuuE6d0GhLypEZAeq7pEOvwxmXrR+3G6YaaY8AIM9Lp1+/tNeHUJk3QElnFOhY0Xh4bfc508zZsprx4HtkK71Frt9z93PJRWEW//0aDHx/l/kNvUkaS9H8xvX4vPqtHyfIAAAAABJRU5ErkJggg==",
    "redo-2-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACnUlEQVR42q1UQUhUURQ97/3/debPmI6IkKvATbiIatEuyCJwke3mE0EZBObGqE0Yle8/xmlygqapRUykoCXiMxcltGjRtKtNK9FFhYTUV8KGydGZcZz/XgtndNIKCe/y3cu5557z7gV2GIwxKoTQFBTBboVS/wHGmKIAEA0Ntz24OzHCOpinzLCyjv6r63rnt1QpRUCI1++rOVu7v+VVZyczOeeyEmwbxWBQaC0t04pzLrfmbrMnp+vqGl/k87nkz3z6FOeXskopQgj5fVbGFOWcSABIJF6a6bTUgEUAgJRevbnZWJqdyZ7fU1s/mM1m3tQUltsdOHnbtpVeBhFBoVmcuOFbgye9ptmXWcwcpkrpgKekgcLcpyKopuWXMun8Hn/t8bQrJ3kvPwGA6mXhLG65YTZg1dc1jK1kM1+K0o1ThVVCqJKQIAQEEkpJSXXN6HKl66FQr9dp2JvqxyKj++L9zwuxyNgkY6Lqbybci4zGHsUmVbTv6ZVK9/T5+SYNgFwrFrt8Pq+R+pG6zMOdhXg8Xp1KHXCb5j8SZ2+AmOZStbZmPq4PNJ5ZWPx6tedmx33GkjrnrcWSwEkdAPpDw+N3+ND3kvmk7Gg5Hw0NdQ8nkipiD3VXvv/pH0lKqSGE0BizN9y07WMuAKiiMeEszLVdtzseBoNC22BSik1UhfeBQIP1eerbER7i7xKJhOE4jrJtG0IIZVmWA8BhjFHOLXerdqS8N9HogN9wfVOaZsjc6nJ7T++F6a3FQgjNsraDYFMHRjnnMswGWkyPP2lUVTWuZDMfiMIsgKLHawbyudzItd5zz4JBoY2PbwejAFDemxv84sxKJn2wUChENKqvEUKPKkJaoXAIBIH1Fdrhzdm1W8MYo4wl9fL52GmTXwNJPCQ4Z0VXAAAAAElFTkSuQmCC",
    "redo-2-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACLElEQVR42pWTTUhUURTHz7nvPebNvMExp9SxqLVILVpKEIKLCCUiZnJl7dxl4CalvBzFGlEmiDbzkFIroreolVSLUNq0DCUi6YNgaELUcb7Hcd7c00KNIUbIs7tw+Z3f//K/AP85UkoBh52pqXkrOvb0dD1AXRozYzweNwAYucjdzcHmlcmx2UEiUlIu6nW3hMOO9i89Ho8b0xPPZmcevuEozd2sNcDaTESkdlXfWkqVhFnM4bavzLABoDX6nvj9DZfSudTgrdv9D8JhR8N9TUTkcfmo0/J47yqATlV1tT04IiIo5pRAaLB8DXomnz47PHrtIzqOo0UikeokzZ0PBIJL+UJ22XXdGUTYYdylAkOBleo70ni0J5NNv1NG4UqplMyhlFK0tbVp2XVr2TA8uc/fU+dse6BSm3vizkxXIBBcqLjlD8oQPUNDkRIzo05Eamp8/pRpettz+XyvbQ9UpHxsNjVlOZU6U7X0RHdL84nXm5tr7zM/v/SSTSUppUBEJQAAGJRgABACy8yMHR2WCoVCLsCSQoDVbG7rRmI9eYFsKtY+LDIzxmIxU223fhWorQyN9F08uDKMAMj7J5RS6kTk3qPZyy3Hjr/cSm8s7JS3pw2PSJrCZ2RLxUoZTv4AWFL7G/82jIhcKaUYltdf/V77dRUQ270+/6JAz6ru9X7SNHxB1OXW88DaSiLuKt2PPm/fqVZbTd2EQrGYGKH+b3t3+cBEjuNoh/k0fwDhIAY04/Fp2wAAAABJRU5ErkJggg==",
    "redo-2-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACTklEQVR42q2Uv2tTURTHv/fe916TEIda82J0qIOQ+hpQG3CTICJkaAvFvP4QdJLSpaJ/QQgIjipuDk4WlNcOYnFwaJtJl0w1ER3EqdGUKi0h6UvevcchSX3GtFroGe8593POPed7LvDfluU2bAGA4Qjt8LAsshwArIidTkRnFgYHU4FOhf44/o+sbA1rHCDGGYI6D9w4Vo+9icVGQ0BO+WE9SrQFYFEr8E+zzIlxnYdfSdVc3RLV0XJ5udZmEOtuaAeQjM2Gqo1vouMxPKkNbYd2ihG6pYvAM081VgKiNlYoJ3eB3G+QDVs4cOSQOXFNg3GfQCMANIA6ScHAQKR2CQRdBAOedFeKm85VIMu1TiUOcvLcieuTBg+9lMr9qogeM5ALMGrBiBFARMQ5F3MEChDwtus5QDw6dSYRnWkkzOnXlmUb+00gEZ16eP7kTRo2M3f997UklkUBUBrRnOBCr8O987nkNM4i3XcadVlFnNXxk8kBt08XwaeGCE+73va9YmXxUQopLY+cBwBIIaUBwLBpO1bErvhH7/db0cz8yKnbZEUy8/7zHjoiBQa9tQbZvSHkkZcAID1tyW3upEubi08AW+SR9/ygPSqBvTdEaHLdrF1CJfcuiVk9jE8EACZMcrZebADYaEtE9lQvAMQHxsO6CK4DXEnWGPv4fan4l1TbEjlg+VpCtI7bFtfEKufC9FSjwEBfwOBxGP1EzYUPFef5frB2j1p7U/rhlDzlXpDKe8CINwF2GcSuALioiPoPs+v8CL+aLG+Ntht6cJJfBoHkc8zOIGUAAAAASUVORK5CYII=",
    "redo-2-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACAElEQVR42pWTzWsTQRjGn/nYzSaN8dB2U7AoglhKqqC5FWUr9KBSEZWhHvwbLHjyFnrx5MmjVy/KUr209CS6HlSQIi3BWpRc/Iimwa+YbnZ3PjwkK62tYp/bzLzPO795eAf4b1UodqujxSt9I644slODv3UjZZQtACQx4WSO5VdKA2IGmNUePJ4Wsa0ewQBBgMDUUdcAcKB9shblWvst3ne9P3fo+4v24tMuQWDI1jfN6hQ1kjZVJiKMtAwAcO7c4cQ5H8twZrXp3wIES80EgBkZvDTOCb8BmHHAsN4+6R1/AVBgNMMTGR5fbc695AKC+fDV2IDwGMs+Vjpa1sZcA0EMwHTNug2DyxbLTUkVPowUrQEVSoAKLaPOQvfrMgNr2Y3CiSXcTjYnUeq/eIrx7II28lne1lPP3/shAEIA4HBRHHSIXYtN59zrz3PzHjznA7JmH0K1PuhOOlZhMZY/n2RY+8xSfX4jzYcCgKUphTGAoREA4sLVx7BHBpjQkmJNqs7Vb7Y+vdmYBkWGh4WzNyZvCOhKtXH37D9mhvRy6C48eDxAIMdcccFi+fux2liAkjcVUR8zzLEiZZJi81MtwIROb/w9YQECCYBWG/6DTvJjGgSjlNuPOHPWCLGrjOp7vZodMbYhlYamR2HMEAdHopN3r9b9t38ib5OAYLv5NL8AgArPOaNqAKAAAAAASUVORK5CYII=",
    "rotate-ccw@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADM0lEQVR42pVUTWhcVRT+zn133iSZiZnQWNSFwZAYOqGlkzfYibVOgkhdFVReSpMIFkJXilCKGxevD8G/hYt0151WRDOIC0FNLSRDh0wxmYKQDKSt2BaxddpUGiczmbnv3uNiSJoOycJvee85H+d85zsHaIIHT3hggV2QTs9KgKn5nQBgIjHfS9Iaif3T+eW5G8/XAODtQ/k+re0jzKZbo3ZXUtv1jcrDK5niSLmRygQQP0Y0lsi9/0Trs5+uqztd60E5GpEdU7ZoO6ZZIdBVgICwjKEerD0E8Pnte8ufZW+d3PDgCR++AQABACT470q9pLUJJiNWxw0wXt7Qax8E6kEfW+ioSbOnou4dMawvhmXM79574NLrB2b2PqoMkABAbAUkJEmiT5h15sLiC6M7yJMDkDuezL7YRp0/RUKdM8BZx8NZ8sGN1sad+Xcj9lNT6/W7htlMh2Q0HATVm71Xfz5TdAcok1lmFwPU6fSI84WkcgdnnA67e7GiVt/7unB4Kp2elQIAmPmPmv73GgMrIEqD+TDAXT58E88sM+CbDEb1+UJSnXIWQ5mrRwsVdf97IutjNzXfms2OaPxfpNOzksE0nshNTA5d5/HBK6ktsSecy6++lVz4yk3Nt24m8A5eAYDh7LAhEMPCgjGKifW+LSJw6Llo+JlxqbAfAFx32qJtHmlyLBqJ1AYwALqzRWR08GtdlzVBPM1gKpWepN1am5ubEx48oZkGhbCJLbm6RcRS/amCMjOrNwnE/eX2XYmGAfjwDdhMVNX9Sl0GS9t2BxgbzJ2bTK2we/BSHADc+JLtutNWw3BMrjttvdb7YxgATiR+GZpMrfCJRO6jhhRsEcDkAVSM52ORaGseTJGqKae/Lbz0+04VjSUv99gUzRvoUnW9xYkXBwJ/094eWPgg88bBi33toa4fAOo3rKa0Vt+pMC1B2Vqiut+i8ChBvGOR/Gujvnr0m99eWfY8Fr5PhrafDx++cZxToX04eZrI+tC22kM1vQZmgxYZQ2Aq0Ky/qFVuns4URx9sX1pqvkWbH8f6c+2xaMsQg3sM6bogWQJ0/sLCodXm2F3AtDmAnfBoAI/jP8dTdnPd2o2HAAAAAElFTkSuQmCC",
    "rotate-ccw@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACWklEQVR42pVT3UuTcRg9z+99t14z1AVaSIHSpUTN176M2gqMLoP4yZwiREUF/g9jlxF0Z2hCEBiNvRDe9KVQM1difpARGgShEZRbGfNrm+9+v6cL2xIJoXP7PIfzcM55gE2QMm5gG2ydEwC026P9YJ5+MNV8K+QfrDWNqqvQqkWDXdPwLmrNYwvubPfQ+87VCFhEQRoABAAw62bN+kTYHrte5tn7BdBdmnSKgIxS6wagu2qtQ3Nt/pFrUZAuXmBu6POMZVacXtdrdoGzN/onjvdtuZg67PGHVTvre0KNiV8xJxiXkg0CgLD9erLSqmtcys3Pe4zypKvWZr/uyt2sSVcLX1mO707aBYC4s2liABCnyM0cuD8dzAgAIC0eZd3UMwCfwTgIYh4ePlNAw4y6O9nkSjgCAAr575dIiN2uaXUCxACAkD9Z2370ScV2TkfAAgDCjcmJsP3meckwg8QIlO+OlHFDyg/e4uJmJAIJEUFEEIlPAKr/uk2YA+t9jtOqUqm0LkaxGTU1aY4iqjWr/QCyJbJgfkokGgAgMRxUW5UDgZdm3JFaHn5VbXl8J8F68E9UTHnvaPcOlyKdR8ZjNE4hACwll9rkOFQgAB3m217Wakl7jB6AiaRkw3FItdnJK1VWfd9K/ttAbu3HZWfm/GKRHDo2userPLc9Rnl4Nb9wLvYuOCRlfCPnYuXa/MmLHqOsl6ErtS6MMak0QVSYwjrLzMvreuVCbCrwQsq44TitikqlR9xw0KqkPVRpka+dWQdAXAdGlogeL69k7g18bPlZJP7ja/i/vuo3vZ0Yr8H/VYEAAAAASUVORK5CYII=",
    "rotate-ccw-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADQ0lEQVR42pWUTWicVRSG3/N9d2YyP0nHpMbqQrGYRScUxG6sUkOlbkTcSIJo0S40g5BSbGzB1ni/O2mTTHFsE8EQrIuZKIUJ6iKIKEK7KEb82UgnaI3oQkwsSZNJ5vf7ucfFdOI4ZBa+y3PvfXjve+65QJOklIaUbKCFpLwqAFBznQAgNZZ5CJ44LDqimRMnnq4CQDLxcY/fJw557D2gPXdFCP+v+crqt0oNFQCAmYmI+D+gidHM6e7OPcmV/PJur+hEwpHIVCAQfNZ1Hdh2BUSEUKgd5XIhD6Z3f7r584V0WlWklIZSSgPAnSvQ3xuba57p0ivhSGQJwBPlcuFssbje43d4F9mlrs3C+iGt+atwuF09vC/2tZTvd9edAYCo2WJXmD4i0ITWem747AsDO8RzHcD1idHMY+G28BftgeiXW9XXDliW1QiizmAwbGzkV7X2tDeZnPu0alf+KLpLbywu9lIsluPFxV46cmTdiMdf+iYx8sGTd3fe8wM7s0OnpJqS8qowavbo91KpcJMZv4Coj4HHwbxbKaVjsRwrpfTc3IAXj8edmZkZ39ujr/64ubXxGQlzPJVKBZU67OH/SkopmJkuJGaPzkx+zqlzs49uhz1xPvPUxfErH6VeTwXrB+oh7iBNRMwIfO+6NjvM+7ZBgvFgNNr9otO1Zz8AZLNZs/GNNMqyrBoNbohrgOVtkIbxXblS8gzwvcxMuVyulRtY1jVDSmkI0o/4hJ9I09o2yHCKf1arRfY89zki4uXl+6h1SteglNKe9o5ubq2XjEjgRuPsIJlIvzd9aZ7lm9OxWj3r7+/PmsxMzEz9/Vnz+PHJAACMjVw+OH1pnpMqPVaLgk1iZrIsizrQGxUhWgAQrtrFvtMjx37byc+4/HBvKNSx4Gn3Vr5iHgByrlIWCyJiKSUNq4Hbibemn+mIdM37/aGld85fmXJ09ZOK69wIBnd5cMr7fSQGyDSHAPxlF6vPq3PHbCmlAdC/La4P4ODgoK/n/r6ThmGMtrWFfOVyAcyMcKgdVbsCz3PTW6srJ9XF4duNQ0vNf1F9IXnqcrvvrvBBwNirtW0LQ9wqwV44c+bltea9rURSStFqMXunAc31fwB1SplmHBxCqgAAAABJRU5ErkJggg==",
    "rotate-ccw-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACaUlEQVR42oVTTUhUURg9331vfM74B+UPDAZBUKC0sWVUUNS6iHcxijIjpUWQgZgpPe/IpJONq4Im+1c03gNpI5WLNgYhFNSiaVFILUq0nElT38w4794WNmJD2Nl+3+E7fOccYB1M09awAWz777kOANdCg0MEvGu9wvss606wxAicVdI7qCStaD49oaScnE58vck5X7IsiwkhJAAQAERCg1NKyrearo0bhv9GOu0mpcIEoBgDwBir032G311e7LzU1RAzTVtzHO7pqwJUPFBUvDeTTu1ayaTPtXaeGMhTTP09wyNVVdW3wtaDZIfgtm3bGgHAte5HbyorgnUzs9+++AsDL5dT7gfX+xQBallwOqmaYk1ZIlLRq4+faIztUe7SthbRMM9WD9Poz/nkMyKaUoSdRFBCiGw87njNt5tXHMdhAJBZSJwmok1Zn+8kQAoAEIkMBy1rsHSjT1uWxQCgN/TwdV9o6PnatymTnSjS6JVp2qdqaqABZlYIknl8ZlkWGLGPUqodAMAAQCn6DEK143APeC//QUQ8HldCCCk9uYUI7hqZlHpKRLUAIESXl5O4TrJu27YMh+9WFBeX7JZQ4398VhSNOoXSzcxqmj52sb2+Pj9NnHMPAK6HR0YLfL4DciWz/ULHsVndth3GOXcjocGW8s3lA9GeEWMhtXiGc57IkcPhgSq/VtJfWOA/kkzOHerobpwxTX3V51zkesT9o4YRiCkpy6SXnfQgvzPFSgsMY7+S6lcqtXS4XTS+yCWM1pfCcbjX1hYrqywrO67g7YOirSC4RDSWnv9x73Lv+bnc3n9bk4/81v0GiKcgofARMiAAAAAASUVORK5CYII=",
    "rotate-ccw-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC4ElEQVR42pWUT2hcVRTGf+fe92YSY2Jp508DBTE02MwktjgbK9RRQXRRgjZ9aYttV9JVV124cTEMgqhgF3XnTnGhDCmCC21x0UIxorhrRsG4EEKtE1pNGzqTee/e4+JNxiROBT843Mu953yc851zL/wLNZPaYFSpBoDsPBeAUj7aj/BCd+z+J8vLX28ATOVOTVqrR9T7x0FvGwl/cRp/11xtrG+J1W1E5fzxNzPB6Hvt9T9zMmIetRpeshLOevV4TQAITBbnu2vAxfut2+//xvUOYABPb4Mif8S+7cxQ+Ib14TIqzyV+4y3n48mNJHxMO7ondp0jir9qTbY+Whz/5qnia4WtyQQARjQRRIyx76pqY6n12fwAeW4AN8r5uWeNyXzlNbwCtUpKVE8zQtltTcaoOq+auJniqcvl/PGLUDMRkU0zj2yFc+HS6sK3sWu/aE320HSueR7qvkrVphoVTxwVzAdeEyfoLiG0nuRKs9U4u1UHgArnwh/5KC7lo8uCvLyW1dzKSqPD/0XafpXpQnT60PhZPVCYf6Yv9nQheqlcPPnpvn3R8M7R2InrPO9BVBw/ePVqlKk+kYcnsnbk9bEHzAD0dNFBRLXemoh/JO2W/t4nQvne+a6zgR8HpEVLHlbaNa4ZqBmDedqIEeflTp8oCYIVp7E6mAN0nXX5b6XqXkVPx0n7wdqw3tz6dijnog8P7j2jpd2zJYASUYa0REktsvt5JQtwoPDq4YN7z+hUbu6dTSl6TjUpjTV32eFgUWFEva8urX7+66BcJgvHJrJkF0FbPucqzWYpgboGPVGlea9xdypz7Ki1mS+NMcvlwolL3scL0rU3OxY3FDAjYuZFOA/ccsQnf2oudGvUTB1Utn8fdV+hErbzExdE5O3AZELnuyhKYIbwGqPqP/advy407129uxkzYFb+uXhyz+xoYIYPi8gE4rqKaTm7sfjzrS/u7PR9GGSzAYPRb8A2/A1UbCmEBsBTSwAAAABJRU5ErkJggg==",
    "rotate-ccw-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACIklEQVR42oWTz0tUURTHP+feGZ9ijvhjniEFUYLkTC1qGTUQ1LIiuzEkBFEt+gNaDy6jaGcpQqug8EHaIvohRENtBGdh0CgEUktnSlEZpnHm3dtCn6iEfeGszvd7vod77hd2wWj2gdnTF4BU0jwH5r6Vg4f9XZd647r5rsVdAOoKWXaOGa1WRr4uTVcgp2DYAmiAZGvqEdh4z4F0R0y3TDvCkzjmBTYAJSKXoe1+d2v/WrnydNZgdJGii235F7V456xtnLbU7xVLwfiejSXtZ194OjF6vHtwJfgVTBiM3lzbNwVPt52qNdZ/KhX/Yl19vrtUelDGVy0sugKFBuDSyetTIuqsqlWPza2+XlVbk1/VbfWdwy0CJwCXJ99IQVigUAejAGSjcguRzjDedBNwAPR3ZXv7OocS7IucAhhIDs4O+OY9gALQKvzs6Y0nYPQApiki7kSGTwpyClHfgeS2WIQfTjgEQVikZKNT7ISP72DYOtxhoLotxvFWILVJy4d7nTNkYgETtu+gScaVdwbch0gsbR4jIM1pP/ty8yGGrcHoqPLkGyDOC2XMObumiI0CIgajA4Iw5V+74+nEeK1RmXI1e7u4FixHzmk/24PwWEvsRs1WLi6UJqe37xx9uVTy6qBIfMxBu8PO4FxZkIRS+ryDdUvtSnFp8mNkKLtDEYRHO0x7Syw2hNiM4I44qOLkTfin9mxhfep3xPtvav6Rq139v+sB1vlpQCnwAAAAAElFTkSuQmCC",
    "rotate-cw@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADLElEQVR42pVU0WtbdRT+zu93b25S19RtboPCJpOKWo1bzJh2m4SiIAyxov6mSUQEsYI+KiKKu8ZH/4AJyp5m6cyFuQcZuA2y2FXLSLpZ1iG41eHL5iaRLW3T3tzfOT4kdevWIn6Ph3O+czjfdw5wF4Sy2bKDVWCMaEDozviyAh++KoK4UkFU2DmRdHRXb7jYuN91EptI5JqjF88cDKgOAH627Fyc9QqWZOpwdfdZWk5S5BcfPrI+uWbLxwx+z1UJT8Ag0lDQaPEcmO2BaPHq54fPD/35xo6qWAm/Hanteq1D5CugyCZd7ks49x7Tyn0wbDUOMtkjyjqXiThkSC8pXdCkhwG90LKN14mcLwh0ZmRyV84BhHwA9b6Cd0PfPK6gaCG68cjo5O5f71jDRQA/5lPff6Rim0paJ0YVuTayzXkAcLLZU7pYGYxyPWMfJvTarfXoj76jtb2XTH8phkdh+wMjyJ5SFzZeF3em931PJZ8PbaPbVfe4ntvjzvJ8HACcSmUwMk/9tM613ifN6O+vj9b2XhrOVN2vajtCXOgoVSlRgH02nxm/j4k3AlQP7dwJIrVeBJV/Z85nTg+9PTAjhfT4M4BQW+L/h478KrMY1YVV+AtAEgTCK6e3/WMMFKYDHYtvPsCw5dHJPSMKAEjkKqCoy4lrAJBV+5IAQBCQja3ZEo+53W8BNAgAqp3AVzwnKQshPyYQ2meW4is5O1A+fAW2DylyWQtO3iIinmjZeWKyOQIJpqdX3dHamQdUEUVmy/tbdm7B8+h4p0NJA0A+PVZ6c+dUaB7/YSsADGeqrjGifYjyIcoY0ab/fAwAXn3i5NPvDPwuufTYZwCQzZYdAnzlA/gt9XKPG+dpABJGs6+Mnt3z80oT5bed3h7zusdZonNNveFZbJ4Ig8Aw3X5nJnUilfDWHXJ017bQ3jzGYkeIuNYRLEXkvOSqrpzl1kQjvD703dRz15bO67ajFVUEMWB0IfPBp4rUuzHdvSHiJogUFDlo2eZfIq3939QGvmyrK0QdJWn5G1kia095+ckX0hK1kgKdALlXDlW3n7vlDqElO2A10/33Y8Ndj+0fDepiXtGXHOcAAAAASUVORK5CYII=",
    "rotate-cw@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACVElEQVR42oWTz0tUURTHv+e+N/qk0SZjlEQX7bIobUYbwsh+4KqNBG8Yf0Y/LXIVQcvh/QfRJnUZiuUlqlVWkPNKzGiGgsgIIoSoZCYSzZhxfPeeFjoyGthZHe6Xe7/fe/gcoKhse8zAFrVZp0ITBwsHpNsO3NlW7au/KgRFPJWvFMLwEYvHOZUdku+OZrpD09eZuHEkdbhbFF50QLrj4Mu+GqthFtD9SuUNAhY06zRD9ftLAt87w1OXNXm1DG4HANO22ZCSVCyUiAbKdg/8zv24N5yKdADg4og9za8vGmQ4hDLyOPsZAEwpoc80TARYVNz+szz3aDjVHAOYLoVTvvmsRelgRtcuWTcMsurzesmsKK0JLi5/zQOAAIhXTKuXhKj0lufOAoANKYZSTSvYN6Nc97gHYgZjP0Dvc97PcWIMrkfqDE896QxNJguD22riXYemK7rCyV0AYK6dBYnEpzjiItGaEHChiy/EwWLG/mACM4q/qFuAOgmgruCS1azqHDi6qirDm90ckE6nM1rKqAIjSKBfa38GwPqp5dvRYje+CI5JW7e2TpibnRPuMbUKhogANLoWm0n7pgdYq2ulpjVIoNNw4dk2r9PkSFIOgN7mN3e1VqWGv/omwLSOXKwx0XY+8pF7m5Ijscir6g1Y7h2v7GlKPrjS8o07wpMXVsFigwqESRlVsZB7okT4HxJRuadzzxl6kdgICmFGCGJhRWX7Rt8euV9AmYqhlzKq2vc821nu336OmU+BUAamWSLh5nh+RKbaFmyMGRJR9d+t+VfnDfpfgykEwp63qVYAAAAASUVORK5CYII=",
    "rotate-cw-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADRUlEQVR42n1UX2hbVRj/feecm9ybJq3tXIZ7UITpkGw6Gfo2cOCj4BBzcVQ3VLBQHxQcMbNLT84wZghDilCJRfwzhy4XfOzDLFQHYhHEKdzhg1MfhLradotZkntvcs/xoWbU2O57Ohy+78fv+36/7wMGwhhDUi4KbBP1ep0bY2jw/z8FUkpGRBpAb0aeG6aMszsKwns4t3ZpgxWE+M513fWN3EXhiD/GhdE/nZg+/gNtBlFK6WLx3R3ZkZ1vaK1fTibtpNEaxBg44wjCDrTWs2s3V05Xq69ee6f6uenG3QuFU889IzaDvCXn9gw5I/NciPva7dYH7XbzC8bZ710yEcV6twVrnAk2mR2564Wq+vDZqBv9SkQGAIQxhsrlMmZm5pNxq3GRGKNGs/HAlHr+54Ex/ALgUrU4W0xk7qhblv2Z4DyOgqANAKJc/oorpXpnyh8X0unhe5evr+ypVCauSllP5HKIfd83wGPsypW/zCP7gtec1NAT7U4rY9uONeRkrLUgsAFAKHW4d1bOjTHbnmq2mnOVysTVWq1mTUy4UZ9KPp8jz3Pjgw9+cqc2Jgui9SAIviTiO0D4+pZqseUcGk5lknEYXTDGkOd5enNPnufGAFAsHSsAKGxlC7HhHX2wefNvA93+kYiMMUZvldz3j+d5zPfBh3g4a8gsvl46dp4BAIj+JEYUJjIct4m+Qq7rxmNjoZ1KpV+EocMAwACAYiynnLRJgvb92xrbDszzPCalZNEN7BVCaAazcAtIcCyFUUDdXniUiIzvY1tmCwvXmVJKx4inO0E7YLF9EQAon69zz3PjM+qjesrJHGk01vaWKi/9VqvVrNHRUb0hP5DL5cj3wZVyo0rp/UPZ7N2XVtevqZPyeFnKRUFSSgYAu+z7R7rM8kFkwk70dEGOf7sVo7ff/PRAMmF/o+P4sk7Q40tLiOr1vKbNK3L6VG1/Jj16LplIPtTutOZ13D1vwL4HAIN4v8WtpxJJ52iv2126sbr2pDo7udKv/d/S5vN5/uiBIyXG2KRjp3Z2owhEBM4FwjBY1SaePjE1/l7fDn0lafCMKKV0/52xcw+TjoZBwhHEl185mb8MwAyCbGsXKeVtD9sgAQD4B8JXjn4L/8b8AAAAAElFTkSuQmCC",
    "rotate-cw-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACXElEQVR42oVTO0wUURQ9983sB1iUXZgQonQWWpjYEWOnsbIxJrz4wwQj2cLYsEbUFR+DrIBE/BXCKogBAWdiopVKYWJvbAyTmFhY+QECLCTMzu7MexayuFDgqV5y373n3JNzgTJYlqVhGzQ3b67rpYcQgnHOg1RqoKohsesiY6zJLxYSTNdDgHrrreaz6V4+f+fWxGWQOnDlRstZKk20bR70dY0lKypjPX7Rc6WUnyUAgKTG6GA4HK1zPfeSUsFeArvQ0dkS0y3L0jjnQUaM8fr63UNLy3MvU+kzpwCocol3M5NtGjFT1yNUKOa/AYDOebO8J57VUEXl48WlhTep66dPKqUom8yGfjTECZiVVdqejlA4vM911/RE3DAWFn4VAIABpPxQ6BwRJQori60AYNs2S2aTRcexA9M0fUVQCtgP0JeV1eV3ijC8IWmge+J9X/fzTyXjtnP8gRjfMdgz3rDhtlTKYIx9XW9kAGR5gxCKAbbuOAjyWv4hKRwB0MgAgAiuDGSjaZrScRy1lc00SQKz0rZ5QARDKVpc3xmQUDOxWPWhTGbEsCxLCiH0zcyCmWZXAAAE1kSEqXXZiqLa1JCUsj2CymEiOgHAL08b5zwATAz2Tk8HgR8xiv59QBGVApLpHD0aj9fO5AvupBustqfTbb//MT9J7IjGRhI1xvG5+Z9tHTdbnlqWpW1KWK8YPRyNVr0mRtUFz/sgSa5oYAbT9CZiLOd5a8lrovXV3zVMSeWht20e3L76qDays+68UuoYFCpA6jtB+ziXy73o70/mSv/+ezVbsfXq/gCc4hQvK85s4gAAAABJRU5ErkJggg==",
    "rotate-cw-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC40lEQVR42p2UwWtcVRSHv3Pue2+m2iaYdlIsiBhGYqcdVLIug+BOIUX71BB3Qhe6FIIodoxL/4AKiisp2j5bC4UuqqBTEEshWEtnKppGF0LNTBohJbEz8+49LqaRtE2y8OwO3Pu7v3Pudw48GFKjFrFFpKQOkAcu3ZvWFWYDQHlkeqhYCPss94873F6wdt71l1sr2TJAjVrUKY1Om4Wr15dO/yT3izy16/Du+KHiu2bhLdW4YGaICILgLQez47D24bX2ucWDe1+1EMLJVid7bV1IgVAppWVVd17FPZmH3mcW5IwE+wNHz0z2qWNakKOY3DHrvW7oRyCXW51sKhqUV6dcXi7oSvuCmErI/9nfunXml/vaMA9crA6/8E5Idp5Coi8c6j35GkBUo+YazObJysszqsUn1vxqef7W2RsV0qQFHipW43ttMGoHSva2aPIi1t+lEsdOkzgPq0WAqEEjrwylI4p7z/vup/OdszcmOBrP8Ulv3UqDVCDzJumegI2aseyt/43AbjEaABGAFfVQ5AqFrreTgIzxd5i7p6rMA7Ta2QwwsxkWEYCan8hD17xb/RmwjCxsgZHcZUmb4ChxHLHvWu2vTujdA38BkvidbpDaVjwaQEbmeyNJMdLkDUyeW/92vMjNSAvmsYOApLyi25CtUNeC9MdFNIjZt/8JEfJLPvRFLJ8CrAluK6EFFhRmg2k45kP/jvXkAoPXU5eR+UrpyCmnyeEut8d/XTz3+wQT8RhjIaNiAyctaYJrkfX275k8VIgfudjNb89e75z+oEYtkoGrOtXh34YpWhPEzPpHrrWzHzdzVCmlzziJfkDsysOxf/6xP+llZEE2ztn46GQ1lh2fO4mezkP3vMEJ55kD8EpVVV5SiaeC+UtrdCcXFr9ur4+XbDL57kApfV9E33SalILlgCAihJAvmYVjzU728QYcbNs1AnWtPtp6NgQZEmGH5nrz6tKXVzawIdtx8r8X278O8Eq/Wp+0GAAAAABJRU5ErkJggg==",
    "rotate-cw-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACFElEQVR42oWTz0sVURzFP987M40iWr2csaA24UKf9gNcuGghBK5apNmEUQQFYpArCVrK+w+iTRq1rMypqFXlInrbyFW6SsJV1HtRaD9M39z7bZEvn9qPs7pw7uEczj0XapCQePwTG3lZP44ZKLiDLb0N1u28JEK3Q3MGEyg8WTHuxvz7tNwRJZeBw3Pl9KxZdyy4fHxyWDVaEGFEVT1RFp26kqAj9Rq864yTiwp7QfsA/ITES0lte/PAqdBrGq/Y7/dmS1OnAa2NmI+TIYMUPOOJ1co8gJ+SukPbj+9wJrheyb4+ni1PDQLSRVewzH6JKLmPcXzFSNBuXcUPvbrIZdkqgAHUBtvOIZKT1W/n14oxM8xUOsAWKWZrKQ4IvM7cj6coE7WRnuWjgVfrxf0drbkzTW3N/XuqzgARYt7AmOnhxR/EYyZPsg0SL/RXrhnjvawVLyu6DwouJtat4oKLKDlIrUKEyqcasU4HJjzSujuJUqZcDz3+ZuciRftrGNJtDHcBfEAM/riqGw2dTICcKEJWu7aUggXojAcnFRd+qau/CohU37kt7u8NTcO01ewOyuhsafLD70KbkpyE5lboN/St2KWhudL9mwmJJ9WFpaQ239J/1BA+Emh0zj5XdAmRSDDdAouqleG58sMH1SnLxtGntq2xb5dXF15A9JhAvSILqCkuZ9ntt5/Txeq9//6aLewm/iel4dN3X0D5EAAAAABJRU5ErkJggg==",
    "save@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADI0lEQVR42pVUz2tcVRT+zrnvx7x0Jk5S1ApCa5WiQ2udTMASpG3AUhHqyiFpYhUEU9zpQl0Os/IP0FXQnYU4s63YjbTWJpGYMRC6UIjShWib2jCTN5OZ996993Qxk5A+QehZnnP4zq/vfAAAQAiPYZWKcNpHfRCScvH6C4E7MmFsx4HlHEg3AQXADFIVlApG4yT8YWHt9Hq5LKpeJ/MI0DvF5edJub/4znDe2BhEDIE8WlIETA6MJOjpramFX8/W5kqr7nxjPAEABkgs07SjhvLbO3+fiPX2R1aM1mb7XGyaL+o4LPR06yWdtAud3r/Hte3eyPnPfjtduvn2fGM8KZdFAYDTLyZ+ottSW5+8fbG4eJyISeJw+cr6+U56F+VC7TwPOd/n3Gfq02M3phbqVKtA2AEAIggRUwUV3iCyrjqgYidbny0ttUVATGQBckUsx9R5b0tWL4zo0neBc3Bhunjzt+oare9tX2DxT+lCBg5f24nufcXsviLAGSKcsZCzIHoNwGE/GfKvNi7vtMK/3jISx4qdub3RdqE6atO5svLmNoAP/vf8EK7+TuHs2K0mwAdTQAAwCgAoFGreqfykHzZvC/DkIHYfh7In+IuVDztVkAGEQMsMiBlcbdcYD7ZGo5li43ApOLISx382vSAT+kEY+kE79IJM2LR/tC6Nf9qYGf/pKEAiItyn0L6OCIRrG8eiS8XF0xnv6ZM70b0vBdQiEbJEAgBWEsd3Rz8ziZkC8DkRMQZ8c9LzW4gktmszHlW+/nliKx2fHVv6hEWyaf8AqF+xggpv9LtjnchTc6XV0G8N810vlpGgJ8aNc1HCLLC2n0t9yu8jJAusVFG1M/y6BQBl0Z1f69N/1z4+tdS9D0CIbBVVe1HOWSJy94CIJPKcHE2dvH6EoFyIaA3zxPsTt3JOlOGO0uI7WjajJEvwNUOo/PKPz2Wc4XysO3f2nvbd0uJRocyqYi9vbA+uyiIxbQjsf8TCU1lo24ViH8bGLUtJ8ZuVV++kZCT/hrXRIbFmSAghQCmdIiHIAYaKQWqzZ8Kr9bXJjX169njCltIXAoCH0LV52hyz6mQAAAAASUVORK5CYII=",
    "save@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACNUlEQVR42pWTzUtUURjGn+ece+94RywkS9pUiu7NUTI3Em2i1rPQsb+goI0SrUTcFBEEJu37cOH9E1yElKNFugghiJg2FlqJkjrXmTnnfVs4+bEI6t29i4ff7304B/iPGYOao3sAKAe75jqi8OSEaPWCF+9JWABWoaBCrI2bnKZT4+85lc+rTRJ6ADAA1ZjoIcDroq6cCU/0q2qqim8E1xRYE6mU4uDUk+GedyNJQp/Pz9g6GRBqu/O7LzPacF/Uf5pe7r8KUI4qDueKt7OZs5M3c29bnyeXRsegJgAAAjUA2ar1TbE9GRVyC7PQ4jYAkLQKDSvtqzekhPVsdHqm0F38Or7Mx6YeViUbK229H8vV74+IsBlEG4g20p5XMIhKrWeml/qTcmX9mVLvHGgrqAD+FDHyt7bz+RlrPnNVoPFBeJ+uAigHcwu3QhOec1IFQaqo2iCiiiu9SC4/Hbo4H2IfVg8rLICdwa7FjsawZTKt/dwiWANAGqpojXHY0jKUK74B9BcAe4wMUiL1jU5SX62Z7uRD35cD3b5iTLe5DbITqjWCOAxTVWEoIWkBG4RBZmDgVRDHqU3T2IfeN8BZS1FV1l0PC0NoVFJANgBIsOt/zC1dcQBcHb5Z6FkUpdnyWvEBgkNtoyxZGxe8Lzdb0EjWPSh0z28I1RilkMxYRsbp3t2Mberzsvf64G2LzI0GNp4gg96q21kBcQ0KSxhVKAGI83srpOkkdJaCe0D9gOPDf/5lvwEXiQVPTM67twAAAABJRU5ErkJggg==",
    "save-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABqElEQVR42pWUsWpUURCGv/9mVyHJqigEO42mFXsLGxstbDWdlY8gloIWNpbpfAFFSy3zAoIIqSwsBEFICt24SlzN3s/m3HC8rmgGLnc4c2bOP/PPDABqOISoTf8sapKorgGXgAEwAsZzYpwENpNsqQtJZnX0qGvqZ/9fbhTfYRdnUNCsAyeAC8AV4BFwDfgANIBAir4BPFXbJM9/Q6Y+UNuir6v76tJf6nNE3ewhawadvdSrAVpgAXimfi1IWmBYEN0CrgMvgSfq2yRb3Sv3ywuL6jH1sfpR3VZ3qv8bdaX4jNTv6gaFoVoGSb4At/9Ff5KJOgZOzQt0UAfgaEm5lgb4lmRWeq8BZp2hlql6BnhV+mjS+3aB1+q5JBb//IEoyVS9DFwsNO+Wix2yAXAXuAk8rIHMS83C0r0kn+akfQdY7p/X9Ncz1AAr6qRqSMvoNEBb3bUO1AAmabvGBPaS/Oyh2StqW90d1oGmpSHPFsM+cFwd9RAtF1vU1TJW7+uhPX/Ioe1krK6q6a+Rq8BpYLHQnTlELAE/gB3gRZJ3B/vssIutV7cA/AL09IRCHGw/WgAAAABJRU5ErkJggg==",
    "save-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABUklEQVR42qXSz4rOcRQG8M+Z37zGkMWULA3FDSgll8AluARqNlOyttKkFNPsLbkES9YWNlbGBpEGGZHM+z4Wzjv9TFJy6tT5nvN9znnOH/5BkiyM35WkcAY3cQpTDK0wwzFsVtVmkqGqprBQVcEGLuErLuIb3uBt6zbuJVmvqmmSYUznWZKtJKtJvh+k13+u5ZdszFtY7NgPHGl6h/AoyW7HBkxwGe/wIMnrqrozrxAcxXPcxgpOt65iESeq6iHuY0075+D5INb/Mu0Br7A8BsOsJ38VJ+fb6MSF7araSjJp3z54wJde2V186jmMwceTPMHn+Rp/q9x9T3Guql6O6C5jF2c7qYM9V+uApSSLbU9xuO2M/u+DJ30YO83gfVXtYa/jH5PMup3pQdrbuNIrWsCtJDttz7DU9nVcwOM/3fb5PtGVEc3qBB/6kJ7iBl74H/kJi+WW4Ylxd+IAAAAASUVORK5CYII=",
    "scissors@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADIElEQVR42pWUz28bRRiG35md9Y+osVulCAmC0lZcet1xGuIKlvAPlMtK4HUaVUBFD6QSpWmkCiYLFUpBqoAjXBpsX+ILCImru1XWcajdEwcOpVSVoja0Uqm7dZK1d4bD1mVrEiS+y0izM89ov/eZAXYv8h/fYFnLmmUta7HF6skGomyj+WqCDR3sSv9GuXmkHgOqOERAUQdExueoAAhAVMHwlvYkR64C4dKQPuIVeb1kmjUmnh4UlWnWmAMibaN+tmB430RgQakDIt8yvDfSiZHj/vYdsRW2D/vbdxbSieeLo37iTQdEWpbSAECYNea6Uz2br3yQSY99AeCFCL8QDW8b3sVibi2In1zkjbBgrCwBAOdNXZg1BgAFfnX2/fxtVeRrpf5vAoowAKAgjbS+X7f5ynkE3SWlJ99JsIzq9O4tCwh66/4jzWlNbRW4N5tJvvS1390ol1sT0wKCOoACiCICgl4xX6ejfvrbFMue6MlNUKKDEA1B7+GX5dbkHADYxurp4dSLX/ndjXL52vh01PAI8q+IC7x+NKkNj3dl+xcFemxv6uDcg82bn0EhzKbHFvzt9e9LzSMzgxAMRjo4V+T1T09M/Krem7yh7Nzapb4/GEgyas+TcnagKxDJaAqd4M/vKs2JDwEA1ci5wbUsLuTx8dZrjOgvB/LRbwr0WDZ1YK699celSit/xubexwDNVFqTZy1rWatWLRkHMgEQB0QWuHc5xfbNdGUHupYFAUF789bn5Vb+fNRLkt2bPnTGztXDSjU/b5o15roq7MP+EVIfmXkcbHyy1Xt4OOi2hZRhT0L9DChykjdZpZX/6K/O7xczybFzdq6x6LpTPcvCs33dVUjulQDgJG/q/Qtq5xqLp46uKztXX4yFtLOQSCTeTSeeo53g7o8A8ODQTVmtWtI0a6zivjJv5xrIJg+cKxirQ851MiugKBMQ9Mrw9k+j/r3LKX3/hS59fCHF9sEP7pbX9wQ/CCjqVEkIAK6rQstSWqVK5ou51SFAPb1rz/gwPb42yciw0VOb10vX+Opuz0iU9A4y9p8C/I8SUFTFxPwb+QVf5qhM4N4AAAAASUVORK5CYII=",
    "scissors@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACiElEQVR42qWTX0hTcRTHv+d3791drq2HBHto9eJDRInzLl0a3aSeCvRpERsa9WBR1ItmGsF1EGFl0J+n1pM5F9VbD+HbXORmuCskkTAUoUA00cB0c3e7v18PNiirp87j4fA953y+5wD/DvozJWhLgSADoBnfeEiSHBcE4ZPF83dfmEdmN4tJAIABwSIgfsb3tsbDKqajpr/IABJZf+r0NrVyqCQKKxBokSGPdGgZpdytQ8soERAPaWOdHufuD3kqHAAABgAkqDNnLbyOm42tVn6pXpXd1TkUTgEkzuqjatT0F0Pa2FWP6h3IWV/jbMmaNiCYvCmOKZm5TgZ9iWqJOVpLfMPipEwBwGCyeSOsvbviVr0P14qLsZgZaCtPxABBTGa3CGJZlSqyBBqQSHFA2BcBQeG61DW3uvfRd2thODZxqM2AYGUW9AsUavNPHuclax4SP+eQ3F2WvZauUCoP54srQzGzod2AwSLoE2WIKJPcakpYS6cuNc2LkJaOAICuJ+S/WrWpFJTa628ctfn6PCCddylV3evWYkRQ6ZkC9cRgpj66tTMBgkK+1B5JUkcYk/eV7DwUyYUSz92PZQJdobrxjp2u6iffcjP9MTPQq+sJOZk8ZgMkGEACjN0koh15vn4QJLo5tyxu248NCBafbHi6nMv2u53enrB/vD+ZbC6Vp/65q6gt2RsTLzNNH1EoPJclpwNMqo2AuKFDipuNvauFL3c8qvd6WEvdM/RRydi0ChCCP3A5qlpCWmqIqa73lr02ux3yG0BQJNnHdT0hD2cCPasbnwdcjl1dc6vO/REQJ0BQMPiKyXPeoALHZQieLaBw+/fbLlMm0e5L1ziZMh01/UX8z1f9AIioIPeHPEgTAAAAAElFTkSuQmCC",
    "search@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC4UlEQVR42o1US2xMYRT+zv/fmWn1EdoEXYhg4RE281ca9ZjBRkhIyBWmbVg1NmLRSIjIdTdCdIPYSOy0ic7GQuIZZjCKuuJZC2lEJURFtaRTd+be/1jcXtoxDd/ynJMv3/nOAyiBafZITAPTZAkwlctNCVpgYYM0AOxpchRrb5HWooIEfdZ67EWXk/gU1FnChq3LEoXJXSq3JUIVnTGjZomvXYAIBAFfu/C5eNbNDx9L928aLiWjUHI6TX4q/uBwdazh+Hjxyz3fL5xjQc/hC19K1AG0RcqKo2A9kNdjG9LO2sEpZInEHQMAUvF77fuaB7m18fGZ6TzaqbLxvSufjbapvoF29SQS+BV4RgCTqZzaSiHeaF3su+g0bTNNlkNDGUpmk7+l9y97baT7lxd2qlvxaqPB+emNHOh2Vp8JuwEAtKjc7vbmd5xSd1cDTKHKUvxWr+4/TKncxyDKBIAEAGhgcb4wxNWY8QogzmaTfjmi5MRQCHRbUnRO28qb9QAxwBAAQEwGs0/5OsPDP2DDZgDjhoiJiDejOpj4hCIQf4saNaBhr94CC6tkv0rADF5U8H+4hoh9DMiJAyLWGUkxFPTYVhukH226Gvlrq9Ej7ex6DwAERbcBlDnvNBYtsADAhmmy7E6Tk1L3L1dG606mVCbbfS35MjA9IwFg9uwkh5NpVY/OSRGrcl23Y/JC0wQj3qncApK1N8B+g4/C9ot9TdcmK0qtuDKLovWnZ1YubBvJv811PV2zxjR7ZDptaoDCA7QEYOvtS6/Mr6qaeyFm1Gx0vZH3mv3bGvqrgFCCIus0ex6zvl4ZmbX1pzfa2eWsOmhZLGybmModbGtj7w5CtJ25uBSEWoAGBWTGxdipS08SH1rUw87ainkd38ffn+x62nwo7AqTD5dL3oRlTS0Kl7Il/uDE/rXD3KJ6j2D6v9MjJ/8lC5ZIJO4YFiwBMIW51sbHh3er3s34P5R/ZAHpH/wCReJL3+T++10AAAAASUVORK5CYII=",
    "search@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACYUlEQVR42oVTTUhUURg93/tpnFFzjDL6gX6QNi1i5k2hJb7nziBaBE+acYwQCyIhKHQ7BC0iQyJoUWJtUmTeOiVb6NQojjVSLSSngoogtEgDHWec997X4s1MzyA6q3svnPude865QBlMMcQE/AO6Hhf/PqMSESAGgIgy0yIKYodtmYcFQcwzhFk2sw+HXjfPx8DCdZDtIjPFAPpZPyav+Lc/qpBqw9nCj48M+y2BZEBoqpC2+nOFld6hueN9uh4XDaPNcuTAkRMNpYbPH3uTPxeaPemWdlG5L3coL692NWQ4rDy/5H4CAUA0MN1a6d099iv/+dRIWn2i6ywuLU0SACQSmgUQR4JTd2XR1y0JuV2DqcZFICZIAGATutby376OpJtHVXVCMgxYQAv/mRKHldno90jV3esF6zSAAVXVHDKID9iMGZePXFoZRpsNgAF8iijJ7wA1ARgAgGI0ZIFsr5u0GUwxsEAgGYQ1AFhdzZBDZp6TyKO11o966uo0LpnoGPZKAogXApNB35adtcT0DACqqg6xQya6J0tbK7fV+K8YBlkG2iynMEwP0qECAPLINTdyG8sLZPrGAaZEQrPKUYWDyZ4Lje85qqTu6Eem95Qmnw1NBaJK6unlpkUOB5ODAKCqE1I5qlLw7YGpTlH03CQSdph27h1A3grJvy9nLmdsNpPVnr2d2Y3Fvsfpxl7HgyJK1dMb4l6fdfCMZZtHQcI6Mb34UrU6nki0mFEldavGu79nOfuhf3juxLX/lr98eVFquzJzO6JMx10fY3MsqjopasXdfJ3GhkE2AGYwkSvO3xei94iTESfuAAAAAElFTkSuQmCC",
    "search-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABjUlEQVR42o2UvUpDQRCFz9xcfwpBNIWVWgRREBstLHwBwdJCfAErsVbsrAKxEmwtfAFJpyApxMLWINgoiEXEKqbwlySfhXPDmtxEB5bdZWbPnp05s1KbARl1MSADWJrP2gIjM2v6ekFSTtKgpGdJZTN7ao9Luy3yeQW4Jd0OgNEwPvU5wI4fuADWgBlgClgE9tx3B0x0gAGxzxvJrT1yNA/UgHugD7BWznwzDFSAYpDUGIiC0R+AAWx1FAdYd+eSA8ddGCXsr4BKQMSSN05LQtKNmSGp0eN5kaSSpDEg6/FKgGKXQl1/G5Le/exQIqMEqOpz1m+0bijOICfpU1IlAP8Rn+do0/cDvRQPVIHTXxIINHQCvAJzQRJjHyHIIfAFzHpMCygpb8718QYspzAaAY6d+WVq7wXtMQmce/ADcAQUgBJQBz6AovsLAZFOMF+vAmfAI/AClL3Pxt2/72D51L7rQE8JCkSZd7DdRD9haZthAcysYWZNB4skNSU1gIyZbQM1Sdf6j3X7yNqZfgPLgfDFKmtGewAAAABJRU5ErkJggg==",
    "search-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABRElEQVR42o2TvUoDURCFz2w2hGBlZWGjtVhobaGdP48gNoKFrZC8ghAUxD6WFiI2NoovIBaW/oBFwE4LK4sU2c8iM8vNuoIDy96ZuXNm7jn3Sm6AAZn+MKBRjVkUmhm+XpO0I2lB0lDSg6QzM3sCMjMrVO0ItIBzxvYGXAHXwJfHOr8mCMcLh8BGZdwmcOAA+3UA657ciiSQ+xdHOwUKYMb9LIovgXc/QlmQADWAOS/e83ge7M5Lui9ZdPLcCjMbmdlA0qeklUjk/h9JapsZQJ1M5so0JX1Hj+j8KGkVaI33Tmia+yTLkqYl3QVmIC86Yd2kW5aQZcAN8AJMuW+pVB0HOAFmE5Al4NZz/SCrTutd4MM3PgMDX78CfV/3JqRKHaANbPsEh8BmdAJ6DnD8r8uf5ALgCLgoH0aNLCkIrjXpA5KkH5XabqkNY78cAAAAAElFTkSuQmCC",
    "search-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADDElEQVR42oVUQWhUVxQ99/3/JzOZaaITndC6KMVFU9zpslKiXQoWKvNBRAUXE7rRRYhGk/j+G2MyGSPFphaKmDamcfFnU7ootgudIoVuLNjWpYumEKsxxqRmJvH/924X/g/jOOLZ3ffuO5x377kXaILv+xZeA9/3LWamVnd2YyClFK7ragC4NO7v0ibczkIkBeHhWhj84brugzhPKWUa31IjiVLKlNQ3+xKJ1GR7Kt0TBAFAgBACQRBA63BqdXHBU5/3P2kmo1iy67q6VLx2uiubG1tZfXL7+fPgMrO5a1usBdtZCLEvkWgbMWzub9TX9w6qo/ONZCTlLVupPeG4/LbQ3b3t6+Wnj6f6zxw83qoOo3J6Z6b9rVs6DB9ntqz2FAqFEACIiG3P69WplN+ZBHtLy49+GBg6dNz3feveva0EVCPpHoCKPazc30fl9J5NHZvvrC7qz4joi6g5GgAwcW724JWpG1xW333IzCSltFspis9LxZnfJorXFhrKQyIK3l9ZWWLTlviLiNjzPN3aAL2QUgoi3HRsp3tsbKYLADMzIiK2NRuyrPUQb4BSiplF3XYSwtFWBgA8z4sUsVhuT6bh6GSXlFJ4nketaaoAwATeXl+vbXTk1hZicgEAFqjqOA5q9Wf7lVImm806zRT5fN5SSoUAYFn2JwRU+/r6AimlAMB25KE7JTXzfSbTOVEcnv7lxIljfzIzeZ5nAb3YsWORY8dPnr9+2bET6frGs/5GQ1PEiLTd816yLfEzG/P2hql/evLMkRuNisYHv9rsZDov5XLbDj/4d/7XU2eP7M7nfatScc2L7zaMx9DQl+92ZbZeTaXaP66t/fe31vomwywRiV2WZX+kjQ7B5qd0umN/rbY2OTB8aCB6y6/MGgCUR+cOOJZdCE3wARgdRGKeiKo1Xb8wMnLsn/Lo7GRuyzv9jxYXJk6OHB6UkkWz4UTzmmB+OSk2Zfn8XGnu6m0uF2eHXpr+VjspLnBURwHAKKU4n8+LSqWiL45dPx2Gwd1TZ4/+iDej9SKLmxTjfxIidp+W/9tJAAAAAElFTkSuQmCC",
    "search-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACgElEQVR42oVTTUhUURg9330zDjOjY46oiEi0adEjqCAoELJWRdFuLhQJaWEESlZoheZ9M42/o6VS0BSVmhq8IWhhhbXIRYuIMAjGShBcRBIqis6PP/PebTHOOAjR2XxwP873Hb5zLrAFEkIw/AO6rivb3yijSgDobhk6SlAqDNNQSWFrDPQ5vhZ72ui9MCmEYF6v10yTpZSkaRq53W5rIlb4LNuRc2Z5ZWnaNM1vRLASKWUOh3NHLLLSUN9cEfB4dCUU4gYAwONJygn4h0b6Ai/X7vlfnMiUVl0dtHa1Dl972DMq27X+y5kcAoDOO4PH3XlFb+cXfp+6KSpf67quhMNhAgCvphkgkh2+oT6bLasGiURxXePZP0IIZgEAKeni4tL8rxvN59/EzRmLx+MxOOcSAHRVVaDrmA5H7zKy18TM+GkAj4FyxpLr5S7TMD7R5vmISKZkc85Nzrlxy1s5E1uNzBFYWarHNm9tgKQd2CJtd0UIwRjICokoAMzOThFLysaE1WIrr63ttamqKjM9DQa/WABIK0oP5Obm5xHDewAoLt4tLQBgAXtgtzsvlRbmX+GcdwKAEIJpmiaJaAMAZTtc/kh0+SfZ5DspJRGRkT57hzZQH+wdld2tIz1+f39JanObb2B/V8vw2OCjcdnu7X+yOdiStkr36AoPcaPDN1BlzbK1E1jB+vrqD2JkdzpcO6PR5SnTND7mu4uqFpcXAg2N5xqEkEwBgNBkSAohWJNWN7Fn7+H7LkfOd0DGFaZ8TSSMtsjG1PXb2tVXR8pOOosKSuoPHTzmatL2jf03/CkI8cGSDNTzrk7foJ75MdJIZn08PURV5yTn3Ey2JGVm4C9D0AqfXtm6IgAAAABJRU5ErkJggg==",
    "search-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACuklEQVR42o1US0wTURQ9970pbfnIr5/Awk9QKS0kahcaNULiDhJQwhQwMeKGnSs1kbiojRtN2KgxRqMblxQ/CxeICdKFiZiwMVBZ6ILEoJQqaEILzMy7LqYltQH07GbunfPOPXPuA4qgQ5fYBrkabVUrehkVQEwBQEvduTAro4EVuQAsWmx8nEu/+FbctxWRAKACHr1DCjGsiZIAQ+WaCIotKFb3eG35RvL3+M98f+HHeckq6O0ZcjnKXgG8tMFrfcrINhlm9uAGZ48pZd0UJC4Jd82HYLW+O0ciNmW0olUDgKCve/Bw3UUO+XrvbudRwHP2SIu//1fI3/sljEFHbiICADmPeQ5X65VKc4xavPF2NjVyQYcuvfDKeQwAaCOgjYLwOT5lnn2tcR8Yd0jnNdO1mk5lklM6dJlEkmGr0fsP1Q1wyBs5AYDyKouxqd7b8z7k61ko8JkEABCrRsPKcNbkGQCcQMLCtogKYpogSH+g/kwtAN40myE0BlN5yYqJfyLGLJAlIQWp0vIcua0IxMuaKIEpqmrtjERpByYmcIOyjPXS7+ULeXIBAErRJJEETKsTiKn9mHJskWuZQCKnWHQx8eQ0Hhn2wWChQ5dz6fi0YWZeSnLdbvR1tXzG2Hre9Fa0anbO4hYANPt67wuSZWzw5cJAU44RIe/cPiHkODPXKTa7k0vxsUI9LZUd1ewsu+PUdp3PmivvkqnRk7APUPa4BevRVNW+Rzp3PZHkOG2q9XmAJxj8A0xhQeIUQ5lgvNaks9NSG8MzqZGrUURFDDHObzoDUZFee7ySWp156nEHZkiIvQQ+CojjBDJAFIe23je7+Pyhx91Y4XRUXKlxN7jjqw/e5Kcq2n7+649Fi5ryoQz5IrfC9YMc8ujXsdO9U3QvCZsgKmxf7VqzPzLU7Im04z9B2yW88OkPbpoOY/cO9rwAAAAASUVORK5CYII=",
    "search-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACQElEQVR42oWTTWgTURSFv/veJNOY2lKdpNWNiIWQxh80W2F0ZQURUceCoIsiglAQLLoNgqBocSGahQURBCmOUBG0qKuAgohW0BKruLDgwqaloLSJaZw8F+nUqIgHHg8e3HvOuec++AWBnOIf8PD0n2/SdBuATOehnRg5AiYDVDHywgT1G8U5v9hofrYeFuslRukmFl3bmb1lSctQYGptGD4CNRH2RSx70ImlyjPl/DMPTxcpmlCQXmK8vbnzcDXt9O1ulpYlG9mY7Du1peuoSSf2n2i2IAA9Ca/XtuJjldrXPZOzow88PF2iJAAFCgFgMo53RWlrwBizZqI0Mg0oq+HYHKsGC58nZ0cfuriWjx+EMwhZ3mh12UYGAhb3AsMu7lIxst7A8ya1oSd8/DpgmOZTT+LgDKK2A8MAYTSBGGLNRX+nklNABFgAmGdeVEM14yJqRze9doGkoSnTLMctwKRWT2yL6niHgicArbQaBaCEa5aKxu1E20nwg8bJKUBecb0GSERHztXq398rsR8DUqAQaPB0acH/4sRS5YiOXXTiqY52u+ftXCX/DSDtHNja1brppm2t3FWrV+5PlEbuurjWFFN1CSfq4wdpx+vXSl8QIRGYYFIMMUvb637Uqx+MMU+jVrx/MShfKpb8M5BTGqCxMTk1W86PRzvSV1sC/c6IqQjy2khw3pkuDb4sj91zVqTittV+elXLhraZcv7Rf5c/hItrAWQS3lAm6d1p/hi/xeLiLjdJkjTLWWMEZDnOn01Gz0Xa0pVZAAAAAElFTkSuQmCC",
    "settings@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADGElEQVR42o2USWhUWRSG/3PrDVXJq4gIkoGOdjtBQGN8QWMlELNo7F5I24tSoglocEBwAhfiQguJ4IAgQoui2C7MQLoQaRpE6YXdGONAXgoVCkTFIZpExWhVvaRe1Xu5x4UlVIJJvKuzuPee+9/z/T8wzWo2e8ubzVvl0+2j/Doc/ksAYVREwYOm5beROa0IfQtAGBvLXkzQ+92mtcaJh0FAFNHoOgmA8y5iAojzO2xY1r1pRmDupYTzqpVAmUK9+MhoeqClLVZ7afxbvpylr0UELF5Wx2o8pGHT6GNDBq8QKYsv91bPAoAm804/iF7alPrN4IJFCgKY01t19zBIAkwUQUQ8WxIKsDbjaqE6+2d3zIYr09B8Bhx3eHt7X92FVfX/+cqS6ha/NvNsdsyGKgJQfQZG3Hf/Ujbx+7yHPWkCgI1V3U2qGryckXYjmJMaGdVZTlzvtOrv54toNHuWa8JYnZVJC0RFujA6XTfV3B6ra1O+qKRiQQqkm73b9aDhBYBrALDB7Dkq4GsmgsfMnW1WzQEA9wFgfeXNucKvgImKc5/NFK64MzNQGHjE7LHHtHShkfr0xFYOBvXyQ6nM6+sEIkMvXZ1MvzrjzR/aoz8um0Wa3kckKD3iLI7GV34UEYCi8dCw675fRSRKBJy1h/9v8AS0/bYzcK7Dqv213Qr9YmcGzqpKcFsqZihSlWuIRIn0EvXReGg4ApCYCBZD5DBghSHzOfMB7JsCyPHSQLLyp4LRxJOUdiroL9tpZ950MKigSC9bm3T6/3DnDez9ljQFIFb9t1sUoZWOOJ9+7HrQ8CHXZFeTee81kbKPADeZeXOso6/uAPoAAO/WV96sK/TPfq760y0AnVQAgJiHJHsQqlbTaHZXaKLIzEr7Rpu14jjAJ75SP3H8kj0Q8xAATA1k9uOOBbHa8/FwlNSnpVunAXISi3Dwb8lyTocV+iHHVL8gMblFJjNtU9XtzQWBkj/tzOBBADD0ktbR9OCkplVyw+OJMWKJf7rYEbWKz98KACPO24sJ8aErAhbjY2T8A6YItlvfFWyfAZKnkA/iUqgrAAAAAElFTkSuQmCC",
    "settings@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACyklEQVR42m2TQUjTcRTHv+/3/++/1mbZIRSKvEiFqOVm5aY58iB1KLoY8V9ZEEGKgYdGQdDomHoQCgsqiNFKOtShTh2MpftboFPExPJkVJQIGZvO/ff7/16HJWn0ju99eI/3+DxgQzDFEBMAYAasmgsNH7vMgFUDAMU803pabCwQA8Cp/cP7BIkh21m+JUgMna57W1vEidcP2NCps2raNzBTnY0ErCSA8oVCxr/d5Zsk0GxiPHS8MzztG0hWZ9d4ApjMOmuXrnseKZZBZjnvNcp2r+S/Rx+nG/si/pHoZnd5z4q98IlIrxCkj0qZO/9kIvRZAMQkqJdZ7VVsd2vktrKF7+2P0419lw8ubkmkm3qXCz/aNXJbiu1uZrWXBPUCVDyA6bde68LwxMfqDwPAuQPvyxlGHFDNgHhry+XzgxNN3wCgvX5sWCo79yQdatUBgJTo1vRNU6Y/de9rid0ls85zTaidBZXrcgnPDY3oRTj8pnFHxrijiU1BR8paABBtbawlJhtmcoXFV0R0dE+2hDRyBfOFpWtPx5se5OWvqKGXHCxdgg9Aa66w+Cox2TDT1sYaAUBk/7sqw/BN2TJz/0vJ6uUdGbelae5yR+aiuu7pcVR+4YsvH9yZMW4b+taLtp2tTUw2zAgAYKH6HbU6mkgHO5LJI1KxOklMH1z65odgMQcUTiSTR2Qi3djhqNVRFqofAIo7E5YYqDEDI5d08hySWB2Kj/mPdTTNb7s7UvHTrE+dba8fb5Gce89AJRGG/xjGxIqjRGJWkNHvcD7kdZXFI34renek4ucZf+qK11UWdzgfEmT0E4lZVhz9V1V0hqd9ABAJWC/NQGouXPXMZwZSc5GA9XJ9/b9uDySrszHEhKPkdUFa6S5v5Q9BWqmj5PUYYqKo5l+3BQDcxE21Jj0ADE40TylWLYbmvapYtQxONE+tfR1AXOSB32RnSwfMAQCiAAAAAElFTkSuQmCC",
    "shield-check@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADVUlEQVR42n1UXWhbZRh+vu/8pEkaWGr3o2Bl3ZhYFDSndKWlOwnsQjbFq7SYU3+IF15p50AEvfh6QBSZF1ZB8EaUmirNrS0Fh8npkuhG4ypdYZYxLyzC7Fzp1qTNOSff60WWLm3V5+77eJ/n/d6f5wN2gwHETDOnCggOACNG4diIUTgGAALETTOnAsQasf+CJrEVVqz07it9i/Rq3yJZsdK5/+PsU00PFCK1GjvFoL8d1jvNinvrcxC1hQKH01X3b0fS1sdM1eczV/rvAaAdoWRyWslmh+ujvaXnFRa84Mnqo0GtI+TWKzel9N/MlPtnACAVKz6nKIEJjYe6a976lqaE//RkZSxTHpxJJqcVFdmGoiR6j8F/hDPtfc/bLk+We38AQMnktAIAU9nB702Iua7eF+LEeD+RfB3ABQAzPdllggBxALCM4sKLxqVia5lNEYBYM66JlFH8yjJKt5tntWVgxMADpplTH9+Ms9/a89STjcvGhBjlTaG8fPenJ7dqket/HVyTfJMHCVRvsluyEANAjpPwvyjDd5yEb4NJcX8gXZWzGaa1/RrU7h50nIRPe1ZA3b1EjfvhnmUtFSx+KWV9zr7KvhntvZIJ60eG77mrb60eqN1qpGU+o52htb4IO9dHunTGwZ8IqJFJyyj9GNI6Uxvbf5zLLAx8Et9pBB0AIPcLEVMAyQBiHXMZL7rx0ECd3Fy7/nBi010d+/aXwQlh5lQ4jb4R5GEAtwGAQIznzTy/n+J3Dq1bmHklb8b5ZzdO1JRw9UzVW4tNlU99KgRx24nXAUCYeYVDPUpE1wEgbuYVfujQGjXqkh/oaji6ssFSjpPw3zi+EvjaSWxPLvRdFSBu26Bkz7Jmg8mVDZbS1faoBuVDAGhqPNilWGE2ffIajcQKQ00vPXt8NtBiVIzECkPpk9fIihVmW7kcAGyMQ4C4q3hpX7pLEb1z3jIunweAuRtnao6T8AXGmWVcPh/RO+d96S65ipcWIG5jfK9pG4t39qlMNKqfmAjpHS9tuXduEtzv6sQqKtNeC+od3VX3zuS6uzI2s2StNzn73C8guA1bAkDq6UunVaXtHWLyNMDAiF3069sfTS0OXdwb+x8g9sBjQOqZ8mOjfT8f3e0/2vf9/AOE7XqUcYOijAAAAABJRU5ErkJggg==",
    "shield-check@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAChElEQVR42m2TT0xTQRDGv9m3r0VKsRITI8Z/B0GJKKVUJCCVeCGGkBBS+VOaeuJqTDxwMEESr8Z41ZsCBWpFTEj01DRCkFIsUfFgPEiIGi6IJba0fW/HAy2iMsed77czs/sNIR8eT0QCQDTaYgBAt3OmvFg/0JrK/Xg5lmj6tpeGsEf01c11gsVDi7SXZY3NdZDqH443hP/VEcAEAN2u1906FbUqVs1WaT+RNZKTWfy6a4HttkWWdmSM5IqAFjHU1qtgonkMYCIA6HXOHtdl8RfFuc8MMcVQ4yPxCwuFCn73fAOz1gmoqxrpZxQ2K58sNH8CAPTUzJ0KuBOmrybmLgAMJq93QtvdZsfp6OGAe8n0OeevAIAEAI2YAQgB7Pd4IjIabTHuDIJCQ9fMNteLgw6U92b07CO5RSUACwhhAoDYfTMLpaLRFqO3/o1raIhUvyuul9GRSau0PbAooROpLBGBYdJf8Pahkes7t2Szsi3uc809TsF4ZtUdTVvGunckdjEJQaUAQTH+wGRqghkAadrwu/OpjJm8ZbMc8lukvW0zs9o1stj4FGAiVg4iDQL4uQOzzdhQnGUFPgYQjy423ktnvremjfVLY28vTwQ8kSKAWClx0jBTzCRXAABeL2sA4KudnfbXxZbz0++Yx1v1wbLzZXWxZV/t7HSBE0AIAIiJBqzSURVwJ0bbK6dKCkDo49lse+Vze8CdGLVKRxUTDWybK1SwJxNA3OOMdlqlY1zBXM2p9E0gOwNYmnSx776AdjRjbHQFE55wQZ9/beJBsAgmPOG0sVYNYFFCD0kq/iqhhwAspo216mDCEx4EC4D4v4UobE3ekhXX69/f8LvnK/bKA8BvGD4NQsTtf2AAAAAASUVORK5CYII=",
    "sparkles@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC+UlEQVR42p2UzWucVRTGf+fe9518YCK1JinUhZQZ1EolycxkzFcn4t/wiunEdCGUgiBuRNyYigtRd1JQAy6CTpVk4x9gSadtZpJJMhWFoBLjQpQ0MQhp6Djzzn2Pi5gS41TRZ3EXl3Of+zzPOfcK94cAGvTNxwHmbj2zDsgUU7IWPCn++slHVE2skfhp474MQTBrAcZ7i9nJ9MreZHplb7y3mD10AblU8Ytcsvj9wRmvGdHWVpcAGCsXVNUCGMNFoDAxUHgU1zqtaErQjonkckGk/kpTokJhzAEojYyL3LV9GTIA4CTmeZgTkYZOkdBCTwPplKMkU6i5BDqZLj8k+NuNqPoqgGda31MaXZ8sZ3YAziWLXwJnrqwO9dyzFgSz9vRWl6x1j+kac8jcc1FOF061ecc11N9XAGL2Qa02bp8CdoJg1vKDeU2gPQjUwhxy0J3Dqs6nl040In29zT/28l5t+yTAAy1dP1fD3973jLw9s5zZbNZicv03no55nYOhq44Aw9a09Pi2nWr914/jlaELAOv9xem22MMvhu4uLqrdVqTkS8tCGN25ma+MLkqud/E0Vr/ybbsfuurXiFw1Sslgv5lZTX37F6XJlccj3JlIGET1Wd+2PRW6uyFOek1kXQQS8b8hUWRddM+aZzuGG1FtRGDQmti/WgMWfNt2s97YLeUro4sCKiCHwlY5ny73/MewxRyQjGdKiYm+YhxEZ5YzmxG1PNhIrEmINQmwEeI+nVnObAbBrA0CtVPZee/P56QewHjf9cvtcuwlYsrz/dc/+rxy9mKrkx/rZldENQVQd7tixNtQVC7NoW8eyVXGk8WBVq9zqd6484aCtvvH36rWt87mKyM3zvUvbIB+t1+piSuro/G/R7EPs79YFK2JaE3EgkoMQETK1rSMWdMyJnhLANnsNdusd+az1aFyNdy53Op1vhMzHe/u1X75MH9r+CqAET4QESciLnI6DdDdva3/OA0v9JWeyKXKjx2d+qBvPn7wuR3sNcMfseNQDyWX7LcAAAAASUVORK5CYII=",
    "sparkles@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACz0lEQVR42mWTT0xUVxTGv3Pvfe/NqBVQlKRpBBW7UOOMTKLFEhHbhenaEQsiRjc0kRhNF+zEHcbEhTWxxNjWYlF5GxdN06SmdiIB/00Gm1bUIkrUYlCCxs6M771773FhJBN69r/8vvOdHML/hgkgbktdXebIRZ/nbf63i7c2PAaAbbW/eJXllZ1B4dEJ/86OUMwh6TC6CQAM5Hcxp+KMtPo4ALQncuWVZYuaPfXBMS9Ws6c9caVczbHiCMh21t73pvF8dT58lge4AQAiWWid71adzAeTr+e5Vb3FUJdRqbWx8YpcunQLx8eurTGCstbqXilUB0gkZeQ8sSpsi7tLThTC5wekLvwoAKZ0ekAC4EymSfs+mRBmHQHSSPoWYGE4qDt7e/3LvuzGk4Xoxf4Xr6Z7z95uejlrTqcHZOxR9UrDdr1D3pHIvlH92U21LamhMSVi2nLQraSX/eF63T/vGdWcGK5RinpoXKxl2GoBARCNw5iDABPMYCcJ6gGL09pEaEtdf8rgkUhzF7Wkhjoq4itPvSo+nAHoOKT307kbyYelZwOAXRtGlsMErQAfKosvr5gpPviKmhPDNY6iowRKgPARM1gKdzyK/uvqH2n4tSU5uM1xFvQYG64AGICYYNi/tOau0raxtz5bHUZhvSCvW5fs7IiYjjg4LEnk3tRMPPD9HQYACAC+TA6visUWbg6CZ5f7c59N7Exl2hXc70Oy61wWf2prdl/INZwrLdf305Zak9dWQ+J3Jdwqy3pGa7NRORFZ645aq09JITuERV2x9pO/p6b+oEymyeBdfggW5gtBoqpQnN5EJCtI8Pa+m5vvg82Uq+btMWwny14vvuf7ZDKZLbPgO5jws2Xzbzy++LKxwZRkugSAGDS4wPtwPpEY+mbs4yCNAfm++Vn4fPbTuyCzVUm3y4Ib+3L1owDYCHWoGE7vMyS/BgAfaTv3/94C99BKg614pXMAAAAASUVORK5CYII=",
    "sparkles-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABlklEQVR42p2UO4uTURCGn0mihYXNFgaxcVnxAoqCrrGQFP4Af4usYKM/QhS0sBWrVTsLFbQSxGLRRtYltmshNoKISR4L3283hiReBl5mzm3mzDtzTjFH1Koq1RWAqtpSC2hwCNgLDFjgpB3dV78G/SZA9GN1k0WidqLvq9+CB5k7rD5Vv6hD9aV6pjPH1yj6PPAi9mp0B+hmzw/gALB/1m1aaqlL6lhdC8bq0sS+Z+onJrw3nBRgiB2ryxm/ac4Cy8Dn7L8G7Gv43CFv6lZd9aa/5GBg5rqLyO2pV9R1ddtduZdUW7Eb2VYfqVfVHkCpJ4ANYA/wFngOvALeVdX7qYDHgJPABeAScCqEn24B4+B/Zfd8UlvLdf82tfXQ0ZtJdkr/T2T/5kM90ryrjFfVUZ5JP/a5pl2CTlP+VhZuA5vAB/VufH1Mb50NChgkulU1qqphVY0mI6veUK/Hvpi1gfok2JrXdzudHfnedHa+B4DXwOXYD6PbwHBeQ96aqMidiflZ30j7T1/HcfXodCXUlaYI89IC+Amey+iKchUkVQAAAABJRU5ErkJggg==",
    "sparkles-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABL0lEQVR42o3TP05bQRAG8CW2cOsL8K9wgChyzxE4ASksIgUpQrJIlRSRcowcIogrcAkkFy7oEDE0FAmpAv5R8D30sjgSIz3Nzsx++818u6+UyrAUv4oPWGnVeviM5bIIiFdZn3q0k8R97Cf3Ef3/sfbwE7eYJTcO8Ff8l5q1iw6G+IvvuMPbMB8F+An9BtSpOhhhHtAc+y2CMXp1yx28xh6mOE/+PPE7DGrQOn5ggt/5zrAbpt3ETW2a/esFh5njBt+wUQuY9UbqN9l/2DAf58TbFzBPnpirEdYy26KZ96JJpwYNcIC1xO8rtUcLxF0qeIOr1tyDMNy37nkYQLetQ8lbhZ34r8nPosFFc6//AJPYwiX+4BrbEeokhx03rZZFhs08va1WbqX9Vz1jLaU8ACoEJvQH8zKrAAAAAElFTkSuQmCC",
    "square-pen@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADH0lEQVR42o1UW29UVRhd3z77XDrDHMdRqrEkJlxeMDFTTrmpUMuLRgbeht6Iof4HffClDsY3/wCJ6RMdxPPWFgM+MEhC28QOEnwxxhowFMUWe2HonMs++/Ph9DKlathP+5K9vrW+vdcCAABMeI4xjGGxPv/Q+2lPf3Fq3/qaUhDicmdtb0a4vTEiC9AAiAisLeEi4sbtvTNXJyqo6JJ3IZNH52Vp2CUCIUyWz1frx4YJYDrbObWHDPMHKZx8nDQASgkya9gyj1AtzYzW3zpY8sYyBfHat5bhdq9Gjz42hLVTGtlPArVyWADEmvQ7lnTzkVo4atmJm2GZtyzlRk03F4noFdvWJ8pHvml7kTombOOF7kY8P1C9fexLlYQ/C5IgwJQAQMSccMSmyM6NTB568kxrGp53wXyDuq7a0u15EjwY+PpOz6Xe4o2BnLNr5Gn0p7+vfnRKbDaLSCXKBpjKYKNcZgNgKnljmf2i65ot3RPrIH3FWr/rdIw248XaMqxzFYBFa2khNAPEf3XfIN+npHzEdwrUMe4Ym0z6irX+nLOrGqqV68Hq7GmvPh4An5H8t2dub5/ns29ey5J6abxVTivI/fm7J7+/PxRskGgF0FoQAOz3y8zmjkuOzPesBHODrSCBWq79zXOn2g9l44EDkyOD3q3+bUAkDQEAs970wTbz5VON8OFHl++8W+0r3tyUI387OVE/vZr5ZWfONnNDYBzfzojTD8TM7ykdhaaw7g12TX66w3m1GqiVWlPOlvzpM02AyciaSawaCUB/bANSFHBqGP7ANBxbGM51S+S+COLFsUWeK/nTZ5rpaxKnEshgggSALc22YKfHwOdKh72xbn4n2Lx5sX7493WvVfzUP1klKVr7/1uAGJpNpUIAPFp/+wqAK5slUj9WUNHA+XSrUAh5foHBZGxIYyaS5NCqERdS0GfTYE3KWkkAWHr0cLct8wTWjzfc3+vd2m2LthnNKss6eQxiufVy62BikJbCbmdgicPGgYt3j9/bEiOOdIegk9c14v/JJ8ECBkDiQZAsfOX/+P6vLXn2fMH2XwwB4B/FtXchY8ondwAAAABJRU5ErkJggg==",
    "square-pen@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACYUlEQVR42qXRT0gUURwH8O/vzezuhJNG0GElyMJTEOiOYQeXYQ9BBR2MVlzX0UMnLcM6dIqijhEtFZ5DTRfyEHgo6dAyuoW0+KeLJFipaCL0R211x92d9+uwU6mnoB883rt83vvyfcA/TDTKCgA0GXboUv2HjubadCUAEMAUqxmv9vm0u5Lzx6R0CcQEEAtSdC7yxYHpUzPx0Fi3X61IuDLvuuzMsKTzAiAmhe8R0CiluwjCMgGLArQE0BRJbSEeetNZsa86kZe5vm/bs4cEBU5A0AW1FIyOFNzs88HJcGxv5JiR7jigVfVknaX+gYn6dqsu80QRfnDefe1hLoIUzTRTahWgAkCvHXFajPS1Cq3qQdZZedo/cbKtzcj06YHD1o/cXHdyOvxeeA8QwGzbkeLqco577YgTN9JdO6DVZmT6yrSgteZ87EhOhh9Go88UsTfmyNy57VhotLNcO/roN7SMTH+ZFrTWc5+vFKAl4zWjx4eGmtwdmAgA4oYd9Ct6Iut8+QN1Ldi6vjV/fXAy3ONDPsGK+hIAduDSX4LUswDBKXy/ZdW969W1ytYNZ/7q4FRD4jZYQPJXMAd2Y/Y2ptN+db/f7yufFeRrXtv61DUw0fC43Uxpd0CSCAyCBLxmPcqmmVJpUyYLbm4NQqSKcmMsOWWunKl+ETi4HSDTTKn46fX7F5MKuFu2HSkCGPbWrhJH5krnltq0JC+xWrqHF1RFb4wbb+8DADGICSwBFkwSALiUWPiEfjnvbr7yMJN0x2+wgEOkRJmlKokJzEQAGAwQMUEwQUhmOcyycBNgwv/ML8KoGi5tOE8mAAAAAElFTkSuQmCC",
    "target@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADhUlEQVR42oWUS2xUVRjH/+fcc+fOnVtpa7EtYWcMgYh9DWmh0iYaBuNCSYSQvrES40ZdaLp1Am5M40ZcaQhCpQ8UQnCBxGg0tgXaZqZPBQ1sQLQapq/p3Mfcc87noja22NRvc07Od/7f5vv//gxrKgniJwACGAFAW8WEw6xgOwBQYD04P1WVW/lJLAmwE2B6VcvWD1lpdMRvHiFmvE1QccHtGABI7bkMRoqROtWT2nvxUQ1b+3C4+mpFgVH+sRCxxkAuTpNWV8DoZwAgwtOci5ctUfiMlO6PbpB588vpxPSqlq1emip+qI5GHx8ikkGo3cN9qf3fA8Br9bceA4Az13dlAaAlPvScyZ1LABdeuNTwxUTDZBLEWRJJPvJUq7m1aPG2AWEH/r3KgZlDf7XFRz8Bo9aIURADgLzMZQEMnE/VvtG0+9tSyy6ZVCS9hwuFO+vu9IYMAFpqbrwaNQs/c70HNUpu+8mO+aPCiFUG4XyPBn0DABz8BcssapfKS0eJ9mbC+d2F9va0Hy529qX3neUAMUCd9MP5GwNTiXEzunDKEkWVfpBJ9KafPSYj/GsW0tXedH2HG2QOmsKpWdb+hUtTiXEvnBsG1Mkkkpw1x4d3WkbBrVDlXs9a+kJx6Czk1fJH/emGd5prhk47kbLjACGXnz3Tn2483lI92G2ZxV2m8p1l5JsjhnM6YO4O1hofOmiJ4mu+nq8xiLGoWZ52w0wtkF+yRdltL3z4HgPIjjzxfk7OVhswhS1KR7zwj+cVYS5mlkx6cq6D/2tH85+TtCbJODOJMQ4Q+cQoYOCAIgMyxEbFNdg9TZJBq/hihO5I7XPS8sjnY3W/Lvu/n7UjW7ttUdK9FPzW05duSIXQR0OV4zZFxgDUSu0zbWCE96fqf8nL7H1GuvOr6/uzQX7hshMp7WqpGnyxf7yhM1Tz25Zprnwg3XisKT6YcKzSriBcuPxpao9LpDvzMnt/x8i1u/9Z/xazeMZnGLNEYaUXZs5pUhcBgDF+1BJb2qXyN1z/OkMKiKjvZ6oGZg782b5n9AMCvRUxnBVDKjcL2sSQ6xCxioc1ZCC198oqIm37JkoNGfBzY3WzmyLyKLSOKPvQ5LFEXi1Na9AVUPgdJECmcYBDvGSJLRUbQrtRjLTEbx4ymPEuQcUNZsXAGJT2/z9G1g4DgNVmW8WEo033SSEspj3j7mbB9jdPUQ3UxjWEJgAAAABJRU5ErkJggg==",
    "target@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAC7klEQVR42nWTTWhcZRSGzznf982duXdm2jrREgpiG6WUGiHMbZIasLSIgkEqSibBkK6iq+66a4WhoAsXIkKqYHTj2ChzFSJSCqKJLvxpci/C2KBFSBV/sKSlzaQzk3u/n+PGgvjzLl+e5fMg/LWJiaaIopoFAJgOV/YLxhFELDmEm45dcj4evvJPDv9+1B5aftDLlV9BcI9ZNobBMTKhICkZ6JM0a59qto5evsNTvc4URTU7VV0aD/KVFgKWjE6Hst4vwc7ijrt+wJZvdDqEgKUgX2lNVZfGo6hm63UmBACcHv7mgIflNW27bzficHamunockE8zw72EeJUZXm4khz6aCeN5JfzZlNsHz6+Mfk8AwGDtnHG91UYczk5XLzWVDBYdm3ULvdcs25+V9BdPhHGzEYfPWddbAWvnAIDx2epXg54otVKzEQJ4/X6u7+OevlGVJl1X/p5HN7q/fVqUtM/P7Uk6euMJw9m1QFQS7TYHJQKMaNe1qoRrZgvnu3pjUSv6idD/UZtuX0kFt1JpBji99iECvsQ5NaZ11zDgGAFy4Jy2N25vESDvRRaf5zQ/Lsjr62Z/jAhUO6V2k0BimUgMSZ0GjrNtQN5PwNghUqJSHHQMsA7gHgEhLhiXbhZyd39t3PZWzgTvgbNHnbPfGuV1CHN5YLxCDHBJkS/01tWDgPSC7+1+mg08cBvbA0qWpzLF9xnZ3hd4u59hwjOo9QElfInIX9JC8vB3mW0vC7Hj9YX48MVtfbOZV6W46ErnOunv93gG5zzZl6T6VrQQH75YoOIbmd1cfjceu0wAgCDESUWF4RNh/FYjPjSZ2s5xJLlXUuFFRHm/Nt2n3onD2kwYzwsqDIMQJwEAsV5nOnsW3VT1i/E87Vp0rNc0p8+b5NUkgg/skSNLsr+DgzkonxMkR3v2+pPvJ8cu3DEMJqApIqjZyepnAx6W3wSkY9ZlKYO7jkAVQSrPjP9yG/+vKmIaBaJ+ZPOrBVj9r6r+BH5IhiVcKeyYAAAAAElFTkSuQmCC",
    "trash-2@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC20lEQVR42pWUzWudVRDGf3POed/78VaTaFtsLS5KiyAULVdc2NjQTYUqiItb4UbBigtd1424CPkDLARX6lKophcsYug2DY2pUD8oFEGFgga5JWrS0Pv5vueccZHcGpKq9dnMwMw8MPM8jLADKiD60pMXR/ekj+/tFOsKkCUj8nv+48oX11++PezZOiXDODWl0pr71q4dvBmTn/YfcUnlS2vSA0oABRFLjPlvwXdfGBxu3Ri7edDse7EWpqdREBXugUZtac6Z0njhO29Zm6AaJcagic1mfOxfP//dsZPbZwSg/sT8rnI1Oxx94cB21IZPIeZFYp8vByv9YmAou+AK/dggx33Mj1spV60xse/8L82vn111AKWKez0r7f/gji7jTIqIQzCU1fyJgaT0ACiojUDEmfCDjz12lR4l9JbPAWcdqAzClVnJbw1AP/ShN6eYz42oVRCJGgJgjFgFRWNQZDyxlTPd/sq7qM4qKpsqbeDV2lWdPLr4Hv+BRm2pfuaZG1p/6tIegCnUuKHkjfHFUXqAkWxiYt491q2OGJLJ4HqfDdbzbjUbeaOv/uKtrNOizYOgpG73Q6B/AJjNm2s5igdQRBYWTniN8VCWPjJDYZ5Lqm5vlu6bSdSfXFg44WVTJAk+DP1kdsqoCjCQcLtfrPoYcOtl1vrFmte/fbcD5h8LwRsEB5AM0rv5/ybCmaCqG+tWu0FVvWD0vokUEYAkJmOVdLcDSHtmrJI+7FDsfRJt3Kdev2CV/Nduf+V9a/xVQrbayVvnCiuXN2rosPfuAsNk4J0YRCQO2s3m6QC0gHe29J4F4Bq8cnSxLdZKUJFtRCqucjn37VLbuMqpRm1xSYwYVRE0BsGqijoRVTyIS9/Ow522z/stUJkeurpev2CbzdOh8fRXrzmpfmTElOVfdIjq88J33zz//bFPhrOy7RPo5JH5Ay4dPVRI755ypyaNvdj5efabieWtD+4vuFtXiUAqmPIAAAAASUVORK5CYII=",
    "trash-2@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACQUlEQVR42oWTv2tUaRSGn/fceyczrlFUUESwiKzb6owiaDAIYgq1jGCm2mVJZeU/MIb9G2wEJYWO4HRioTbij0iEjCAWglqILKxauBGTm5m59/uOhRNXlqBP/by8vHCO+IrAgQuC2dhsLJxIyPYG78VEVQusvrzWHb8LLYMLDgJw8Q0XyJv75//IstHLRVjpQTSwWElGq/1y6ff20/G5NW/Y+JVTjcUNnzd+HuxaGblurt/6mR+qRMsGFouRQrfdo/89Oji+vb8n6yzsXv0Wnm48vpPZhnoZ80+mZI+UUsb+P3Lkwg3tTKxKEfM3mf1SK2L+rN09PJm2cHsVF6644lacfU74K8ZCklVd7kIiMijISznnnLBE1BVoGTONxQxguv6g1aw/escPaNbnn0zXH7UBJibupSnA1NSNRK/TjZhXWhP30lcrlbNy/UlipynDTcfmfh3tXX29TE1YbWrqRvLhAxhAp3MmII8gZu8fK4W2pVY7Ch9BOgjUZ+8fK90lx73TORNgGP4/CvpUhtUwsOXC3T8ieut564Y9kVxotagKhHD7adhxgctj3JFaxTYNbESwEykMD0rDU1yv2UuQJwrXQuxNtp+fWor4SfPsIsgl7+FK1uz0v6GemNJsprGYXeoeeAu8Bbj+9MgdgJnGYpZTbo8KLwCWl18q/XesEekCcDdRdj73wftm/XHfRYJ7QErkhJxiJLHqFi/zOYCxsZmo75/ibOPheKrapFNujjEMV0XMDCNbKT2/1e4enV/zvwDnAAwXr8jpwAAAAABJRU5ErkJggg==",
    "trash-2-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC8UlEQVR42pVUQYscRRh9X1V190zcbLo3mYAx3lbxEvwDMccE9DxzEUGDinoRWREWiT0dJckmmLAYEFcUIxHNHMzBnMWTngXxED0YJbuYbGZmY8/OTHVVPQ+7E5d1g/E7Pfi+evXe+4oS/LsEAPPXL6SPzD6+v9frEwCyLJWbv16/VSy+0Z/MbD8EAJLnlAMHlnSWZeG36/aQ0eabKIoPBu8BAEprOGdvBmufefSJ2k+9Xk8tL7/s2wUoEMoOinD25OfXTBwftnb4ikaMIEFIxyTatVhV9se33nnu6E42kOcXp3brmccsRyYy0SB4fEnSRqF2bJwMhayrmoV3avQxFI6QPGIiswuQIKPBjbnipa4BgLqafn7vvsYHq90/YXQEHRsoEYioOzXEG1fWgEADkgjB/2ztGI29D+NWtXwewJwhKadOfXqle/f2mORHtrLXxI6/DgItIqJI7wGIiFYkSXhCDidx8kKvtzqvRF0hKSD/yence5d55uSlt/Efdbb4onnx3FXOz3/S2IgmV2ajRTl9+sMUHlCiHsrz3CTJk3vqqnrWDcdflbi7ntYbx301uPqXu7EiitMEkdbjGZKr7XYbajNzKhU7ACApRVG4qCpns3TfIiM8VUN9/0zaWCTM0aIoXNh04T29iBDAhGjLGjcbw8r2y8GaI5XBeNwrB2suUOR+dtX9k6ACxAAA4e/h/01kdOwJOgCoJ1OeoFObah+IaLLFSEk2PZUaAHBGZ9NTqRGKfiAikgSATqejx1p+76/deZ/O/+CjWrfbv30e3n3X6XS0EuFk9p6DCVhfT2R3rMR5V7ZaLQ9gBcCbW2bnJmChfblUSonzWrYTCeBsCKaMTPT0QvHZ9zBaSQjCQK+UJqEMVSAcEEX61eFoUHodrwCQdrtNIyJsNju6KFqjhXcvvaZ1shSZ+FtRO+yBBBKB995W1r5YFK2y2exoEfGy/UM7ky8dTGrZbOWsAdwW0RvYmCgMXPnLiRPH/yApk3f3N1HkdNs8IO4FAAAAAElFTkSuQmCC",
    "trash-2-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACUklEQVR42n2STUhUURiG3++cc/+mYUKCQshVZNsQok0EwpCLaNlARIoR06ZVhlmQd+64aMZ9ixEEc/qBaRct0o2rFi1bSGGLwhD7AdEhvffMvfd8La5jWOK7PDwP33k/PkIWYmZUKhUKgsBMTzUv2ZbTr7U2juOITqxXxh/dWPR9X1QqFSYiAGDalcHMRERcrzZvel5uVuswYjaCSBjXzbnhzu/R8cnhuS4HAHuy7zdyQH/niFp7ScRnhOecb7e1VSg4MYfxW2MMb6cni4XCL2tsrBTuyfXq/ILreAORjraklKeklIg7ej2bwUxC9FqWA62jr67jepEOP9yfHB4i3/dFTp2+aivrXpx0zhL4MTMRAy4RZ78U3IFBwkR3bMve7MTxw53k8yuUyw0LAGqVeb8ezH/HIZmuNt/Xqk9fZDWXlAKAVqslv3zSeQLbvr+k8mrtGjPfclJc0QqvwTy3nfY9A33zBITXarXk8jIgAKBUKqUADAAEwWDCho/ZjntxIxt4joGBIBhMmLMiu3wm/xsSvKV1mLYLP2MwbxAhOog7UDZMRERUaOfpMG7fY3Y6TAw+oZQlXJ13QOhlohRgAv09kP9lNglADJufJ0k89KB2fdMYvsyxeQIQgxExs+zyaq8nQUpSVqPcsG5PjKwCWAWACX9kAQAa5YbVlvK4SdKPALC+vkKqWOwxMzMAkC5KZd9t9+V+1KeamhiSwSmBJBPSNoRjKbtHJ9EcABSLPaa7EALANX/2gu3lh9IkOWpMutvKQAgBodR2qsM34/7ouy7/B0p8IHpHG7s5AAAAAElFTkSuQmCC",
    "trash-2-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACpElEQVR42pWUwWvUVxDHPzPv7e4vSWuMuEkpQkHiwYT00qvVm4UqlJb+epFCFQ/t2V5KD+IfUCH01PbYg8pCizR/QHNpTz0IGgsFwaoEm9REXTfm93tvxsPuBlkjrXN5w3szX77fmXkjvGgC+FuTH+ydbOyZrnniAA0m5GH96J/bD69uDmNGkwbneXmHpXCQg3ajzYKK/qISDzg2CFDM8z13OzG35tdvcUv/4GSGCw647MKIuXa5pBKOJEmfBxR3FxP36GHR3K6trHWO7yaDuXb5mns4pG7RG/6EzCXwipa8Z1ZIYU+1imQq/wGXo5L1qLZsPNXBpMi3V+52HsQB4GdFc/zbKnUJHhBVEIEk/4JjtIgJUAd3XH3FslE0x6nqxxeBcxGQKnCF9Hhb4LtsaQnkJ3EC6uJOBhAhYOIuZOBI0HC6yr2vTPKVYYl26jQ//Ykf3l9+zX/YXPvj8u2ZUz77Rtnu35zXoTRZmDyxt98fJo5xLN5/c2oy5uKUEi8n2+6phjNebf28f2Nzdd1lD8BYzT5gHUAHQF7FkABUkWWWk+Qw2wzji9mqd4Pk6WaYWCQ0jy+znNC+lKzk4TzpLswdIKZ6s7bthHqkKDYGvrxMrr7swVxVIALUqbfjvzJQULLjCaDV8L4v4q8CJAB1CFMNHYsA/rQ51dCx6E74v0COQUkZPNvfde5948rvjYoHde5eNKt/LSkDJj76aXd0Z68kMiaGdzt0MuusAl8+F3sO4E/gMGU3IOKeZRRI6tdnqtZWrxtU3p+f+eg3c1EIokKG7OYhQh60Wr/IXnVFwmq/FBf67SwpQ4dOnm+Xn6qE7xEtRGSE/GAyRHC3Klk6e3Ot8+MwV0YX2sK+Dw9Yozmr7jGNwERARC1XW3/d2Lh65/kF9wwVZTn2d057XQAAAABJRU5ErkJggg==",
    "trash-2-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACDUlEQVR42n2Tv2vTURTFP+d9v9+kEVGMJlJwqiC0WcTgIIjZLKKCYL+Dm4p0cvIf6ODf4CJYOmgHgw7i0m4RHBw6SEkFJ+lSTcQ2UG1+fN+7DmlKpMWz3OWce+499z0xhMZqqJTuXpeLL1jwQS5yFrKvzfabVcABts+1kWgktOkz6cMkSl54G3QxHCJEyk1kof9go11fMpD2G7iRsjp5q1CjFjvHDR+y9U6OYs/nSp0cRW/9D4j7NWrxlXPpxLgbM+V0JSK65Mk6jui8JIL5rdE0oEmnCB8G3yKXFLxlnzda9dkYFpy0sYhZEXTRzD8NJiGbABmYMPWDZZmkxxB2JBYBR5X5ZOg+t1ApzX3nP6iU00/TpbllgBq1OAZISaMmOm4KuRq1+GepfA/Zo16Wv52P++/MWCq1Wy/bRkGokJJGLVrDwOrUPVgAaNDIkJ12Sq4VAYzLEpcaNDINr2ND/lja45DoeMu8O9YfmPgl6B7Fc0dvZxKmgd+TALOjee6QJygEnZUi1/9zIg9MmvD7ZxM6eGH/imWWAUasVxay2fXO8g6mmwx4BphJXYxoxI8PBjUiySVV5pO1reebwCZAs/16BaDKfNJjpxwIXwB22VU8xXZYAxxh1Sl5slfe/lEh7Q2b4Ue1y3beKT5l+CWAKabC+G+ymdKdq87lZ83CycPhRL+D9d83228/jvh/AVvO4lpWBd5EAAAAAElFTkSuQmCC",
    "undo-2@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACZ0lEQVR42p1Tz08TYRB9823XQoVKE+tBjQcvRE622wPioRpNjAe86NaW4oFIkET/h4/Gi0dPJmLiQS0iq56NHOiGH1rCgjGm0Rg9cDFAAgo0benujocCtqSgMpcv+fLmzbyZN0D9IF1nRYIF9h9M+8kSu5HciMw86o686wEAXR9R/oOISQIEEHdr2af+huO9zNwEgBYXgwSA/lFOpZvuSHaov2OeE9r4HQCQkDVd7zE7JgkWDKak9v5Zb/tX7gpP3v5b6Z1ySddZMQxyktrUY58a7FnfWMgTaBGAH0TONpDBJBRHQJkscv7eiBWdlWCRArkAINoM8CbWLNmrjkd4G0BuDqC3BJgEHgM4w8Smy860y85ln9JixU+PJVIgd0smVeZQYY6Hx2ONamDY4dLLJ9NarJ6kK60TzS1+b9ojDnYWy6uRodl2S9dHlO1N9GkP1UHrVjkezsT83hMvivayWXbXrtmFpdVA40n+0rTGrevNNGhFyro2eqhRHF5y3MLrtNURj0bHPDXV+rQZFQASIfNqf8c8d2lT9wGgGqiDFQDoCk+MJrWpTwDAYKpZ46AVKes6K8/noq9W8t/OO8J+AACmeW5r6NQWzVRUEI4wuAQAA7t5rNonUrKQkEKi8gLA9VDm1M32zzUd014+MYyYs/M/EcqeOeBRhwHylux8eHju7A+JAapLJCWLVIrcZGjygkf13bXtosJgIsIxnxo8WnR+fS+Wf3YaHy7mACmAlOupR5TLGbR5iQFiCjLcZSJBREq2YK+8WdhYSY9+vJSvNuS+Y+cN7nnRDCYCIDdxmWhG/NkicTX2N7pWDBWQRZeDAAAAAElFTkSuQmCC",
    "undo-2@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACHUlEQVR42p1TS2sTURg939x5NC80RaGIbmpb6KIPO0VFKWkRVFyJMDU2aVel4N6Vm5jf0IWIy9rG1oVLI120QzFSTSQUQQsKIhQVfNE0ZiaZuZ+LJCXWQtGzvOe75/Cdey6wBymwgv9B86JlLvcl+4uhg+Z3XWbMvJYGyfjQ6ni70b0hReUSwGSaeQ1gOtAxPrQ2Pn12k6eGC9m9zimkFMtaEq1nZGFJPIIlE0PPLV2NPPRkdb3m/LjqS1FGwBMR47is7rjywcZguSmSRloCAAFAcnBtQDfai1Wv5DP8bYCidUoyQEyk+gpRzq1t314sXsjV1yCmFFj5ZBbayqjdD+pHJsrulxwx7jJREGAGiJikrsKYNrTDA2Xv62gmf862LBbUVAGA5PCLeV2EJxzvW2KhMLLQut+MeU9zldPPPOmGu8KVftirspEiUwqgNEhOmuuZQ8HO+PfKuytbIWc5EKgIoAvZbI+bMO2Lmog+df1Sb6Zw/m3jqYjTAAPAXOHMjZKzdRPwNkftURmJ7HiRj1UGmFjCARiCoO4G1po+GiL7YXL45RNm2Rc9Gj05m+2uqnt4BphisVXR8Ut0GohoLko1VOmYrhm3QnrH5ZLz4dpstseNxVZU5e/C3CHbHvNUX13U1fBrA8HNNj28QqT0/nTeX59/NfIYSCm2PebtU7t6+slTuS5FC5zwfBdC1T7PrZtvWvl//jiWxX/U8zeCat69SBIFFAAAAABJRU5ErkJggg==",
    "undo-2-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACkUlEQVR42p1Uv09TcRD/3HutrYptGlC0/gWWzbi4YZwYnLRPjQkJStpBBwZBjcr3faliKRJlIWlRJIUKPtTJweCAHRw0wckyOLioEPwVkPZZad87ByzQpijhpvve5Xv3ubvPnYrqQoZhqIFAgNLpNGOLQlv5pKx/MDMxrwC40z022BtJtgBAMGiomw7EzKTrOhER3+4eG6mt29vK4BoAFAhkaLNIiZkJAPpuph4ODUxyVA5fAAAhRBlqwzDUSttqECGEwszUe2N0NN7/jGOR5Pn/Za4slwzDUDVNs2JdI0Mer69lYeF7jkj5QoCHCdYaZLCiKpaiqK/MvBm9Js++FUIoUkobAJRMJsgAYJOdNs2s5XRuczPsGSg0SYQ0MU8B/JKJ07Ztv7Ftq8lT45nu7rx/Wkppl8qkUh+klHa0a1ir2eEZLxYLj9sun9SqldTTfm+Xw7cz5XZtP/Yrlzt0sfPMdDBoqKuTCIXizkQiXIiKYa22rv7RUnYxbeetE0t4/9M/5+fZfbPs9/spHA4XopfiXpfP97WwnH/a0dl8SogpR1m2UCjuBIBb8sHxoYFJjkWSd/8idqyfGgD0yOSLWCT5rkSdsjEmEuGCYRjqFdHyZH7+85FikQcAQNf1UtMpk8kQADCwB8DvFT+oKsmEYEVKsks6oK/6pJR217XBA3W19TPZ7EJ/x/XmNiGmHBuy1QgaqjahWZX2mEwddrqd4wrIlTcXD3bo5+Z0XSfHRog0SVaPHDnqcrsjy8t5lcFEjP0er9efM7MfzHyu6apsnTXxUZFS2lUDNTRMEAAoBB8R7QbzD4UUIlV9bZpLz2e/fUr19bXn1hNyy1K5b//c6NIi67pOa9eiEbreaBFR2cH7A7t9LrEfoGPiAAAAAElFTkSuQmCC",
    "undo-2-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACP0lEQVR42p1TzU8TcRB9s7st7bYUWoNSiBfRAwf0XyAxAeVkSEoEDqIxVA9CSEpC8OO320PBQLyavRjEWHGJZ0P0gAdIvBkuaLxpoqKk0lLox+7+xgNCkECMvuvMy8x78wY4ACGEgv+BEKwAQPrOo7apqdnQ3/r3pliW5TNNkpNipqepsWkFJaUTYLIsy8fMdCRZCKEkk0lnMj3TUxc99rywmV9AUC4AxMlk0iEiFkIoiYSt7idTImGr8/M9cir9JBHQQ3OO47wtFQqXqv7KVrDsUyl2QipKUI6Odm7tDjJNUwIAAcBEevZcbaj2Xam07UnpFYgoSkRgZgbAiqp5CrC8VSmN3zWvLTMzERFrQggl5tV/LGzns5FIrG8jv74KpoeSpU7EzEzkOo5f07TrDdHjS/fNx+1E9Ma2bZUAJoAYAKYz2afBgN63Wcz3j927kt2vb3DQ8rW2RJdcpxouOs1ngUVJAMDMZBgGmaYppzPZZ40NzZe/rn3uKjrNr2KxFRU4g+HhrkpGzHSEw7UL1Wq5NXW7/70CAETEhmEwAKTG+3rXc99vuk7lA9Au4/G4G88Vd/YHyswMhqftGbbffQB8VCgeTMy9lNJr84WjLUNDF6vagTrvSFhUa/DpVCSo+8py23EqsslfE0jV18UurP340p0a7qrkckJTDwuOaV71Os53v9bDkbTrylt+f2CAwdpG/ueNcWPgxe9be4fFjgBwRsyeDun6ybJbhl9Vv42M9a7umktE/M+PY9t/xvMX6jIFHIpS/x4AAAAASUVORK5CYII=",
    "undo-2-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACH0lEQVR42qVUz2sTQRT+3my2NSUq0Wwa9SZIyCZN0Zw8BfHkQRE3k1T8AQHBg/4Pa/DisaceFLwoUl2s4FEPdg8eFNZTCebitZBqD0XFZLvzPExTs5LUYB8s7Mx8873vfe/tAqODJKQBuAL7CPqfS2IcSWm28ci2ak0A0OomJyLAJQBctBpPpozULQApANRFlyZVugssztafnT7WZNuSd/VR3Ke9vCN9wFS05NP53E22LefOv3PHyyUJaXjwomJGPk4kks1+9PMHgboEHGIgGsIyEUUC4n2oeg8+f331SQtoKQAQHmzWKPiR6kdCGAcA1QboDRF8EN4R8SoR+8zqI0NdMI1kYGecq5okVqZeFDJOvZy7puayCy/GFZQ/eulgKdt4Xc5d57mMrAx82+1EBRUzQBAWMk592kw9D6NffmiIWmJ9eyuJNKfQ4e/IU4CH4cm0PDwzZWyAo5W1rrdQRTURy1ZBxQSAvHXF2encIgDEgdpk26q9LVpybWBfrI0BglBCGp2NlZf9/tY5xWIJAHz4A9OpCntQRRbEPf16b9yMDRvoij+PHuBTRy4X5nM3Yopp7znxor93S1l5FjCWiWi6v81nOt+W1wGXRhK5cEULLVXIOucTZN5XHBnMTEQ4YYqZ4yH3vnCoLrY3vfaOSjVy1NtoEwAQizRAlnaHCCw+RKp328BmWZO4AoDC/iL+vdFk/yaXtKmrYqiLPAz8DVI2vGGHzcTPAAAAAElFTkSuQmCC",
    "undo-2-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAB9UlEQVR42p2Sy2tTQRTGvzNzJ2miKRSSWxXcaEFj+lDyByQrFTeK4aZaXYrg3o1uwv0rXLj1FUPdmuAqCwMuglKitRBcCGIaI0Jqmtx75+EiaVPa+MAPZnNmvnPOj/mAAyow/J+GxlP25YXF2ZuH/vZ6d0oaaQG4OhnP5aN8Zk3p/gUANKyD/mAusDrqQTKey4esaDFQ/QpnkQoAU0c9AGCGWzl8r5mGhZJOxh1H8NAzDfUGpndFm6neQIILFtNhy9drm496YzRXY2edVCK/xLl4p3SgjNFdIpoZNTfDQwqgmjTy/sa31drIZwgosPTRr1MD+eOh4JEVX2/XGOiBBqIjM8EgxIjd4iy8pGQ/2+iUqg4cTjtdACA1m3/MKbQitXfjQ7v0ZC9fGreFb2+9VpCHE+32YhVZTWP2AgGunreXn4at6WvbfveS3Wm9+oIInwNQRtk7ncidFyxcMXKQbHx/8ZGN2VwDAI128bont+5YLNioIqvPISY/I2YAEBkzAABmWRYm/N8uwiTN28svDcyCN9092WyWfWvfvQFAGWT4ZvzIiTAn4alBwA0/Bm7dFTx6MVA/rzabZS+DjEWTI+rqlO28FSxyVhofBIKG/qSVvLfeWX0+CpeeFDsCYM4knDnBxHEJCRC13reK6/+C9ls5++L5CxSIxrX349AXAAAAAElFTkSuQmCC",
    "upload@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACgklEQVR42p2Sy08TURTGv3PnTqdVWp/gY6EmamKMialDjMGE0kgIC03cFLTVoCz8B0zcjvM3uCEuSFygCC5dGBNsGlpMGiASlYWPEIJGDQQfMJSZzsxxQQcKgfo4q3tvzvnlfPf7gBplgEVwTqUGFPxPJRJZCQAZfbjlml64XP3212VUBtLxXPP1xqJ94+wEXzkz3AEAt/RRdbMZ2myTXC7pdsSHmjVlR5YIwvPLrMkYLZVnO/vHWwaCni1BBliYIL/j9Ivzddq+nONZRZ8dC6BTguSbSGhP60JpJtX/Kvkk6A1m132mCXBazye3hxvyPpff2yWrDUSfQcTK3OQlu/xjKBY5MpjRC1dNkF9tgNiglIlFPTPnLetTcnAyuQimOgLUB9M3l6dnX1+03e/PGTj4j94xZfSR/rQ+8g1gqtUpAIDBtGYtE4NJ10dVgLh6W4DJgCECaCKRldVZq5mjdOPLhxm98LWW0wAgAaBTzx/VoLWGQb33xxpdALy2B0cACm2UbOAuvYu3dQlBE31jTeMCABSILi20s8eCtRcAG2DRkJsNYB6BptYABgHE8+0ZVSpqL5jvVLnGXHLmPCiaBTCZAE7iLQNMUES377itKxCQWQHee3bcYXa/MFF5VRozCSYWzuGZZRTPMQBeGTDRV8SvQJC5KrmC47wgEuoqiIjtkIyR92H/ie6m/NSCHRZRbdkHANuVBACadBkAFuySiGoRf8myD0kZ3e26S9MVEJNE4ZHr2bcj6q4J216ERi4cR66zKLhrFIVjM7aFD6Dsl36SpB6AiVYyQZyKZ49F1Fi777n1PlwAtEUAmQUkBKnzNn9/+njswseqsNZO7Z/SDwC/AaXWHQPyyoGzAAAAAElFTkSuQmCC",
    "upload@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACA0lEQVR42qWSz0tUURTHP+e+O47GFK0CycBdpkhMTyMilCD3tXiUU5Ng0KZttAqeL2jXfxG0SCJoE7jJQccf4EAEQhvBAjOKGghrRn1zTovxx0jZpu/q3MP3Xs73cy78RTHm/nU+VMPDbzxAIZzuHz+/PFPIl4Za+4cqip4HANfz02eK4dL6nQvv7fbA0q/dB6LIgla/7I8Wu4REo/7XpzuyJ8qgx7e1Xgtc1guurb5VHZ58NzIbYy5BFMABxLG5hAkrDpTzuY6ut0BtU2tXvWv3qdXHsUbl2JFTM4VzC2ECFsdNBi0gxBS8IzNV29oYUs0tepdtF/yKBt+uqKYvTDQAsT/oxsQHiI6G5Z6xwYrdCudHDnpjt0vfA+xmAJO7Ib6zMtFYUUlxmENSMImi5czkZN9205uwN3ZhsDw0GpZ7QKxaQRMSbTjzTjKimAex3i9fFcRunJ3pK4bzl/Yzqzx18BiA3uUAIGhoVq2xoSbfARbXas2+d/cVe7m3qkI4Nw98XsttRt3guyFd3zgqVaqdvTn/CWAV/CqkXT/aHpmTsWeViyd3fo1ksbReKl1OS5C28PnYUqcAhfysYAR7wAT74IPctZvh3BPDVMxZM405Z02YJiqCuYzL3dtq/JzauWyijYUH5qiLBBGm3sQEMxEDw0DEBGeCUzN9Zbr9EEz4H/0GPSfkZJFQl9IAAAAASUVORK5CYII=",
    "upload-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABY0lEQVR42p2SPWtUURCGn7l7rzGwlZAiFoLEwjqtIG6XIoVFCKTUxv/hL0m5hWWqxDKks7YSsXGxMSFEyMfuPhbOXU6uSzbZgcsZzsdz531n4I5QqyLvsUyoda5v1Lfl3jKQ1+qV/2I395plIJOETDuwmvt4or5Sx+qJeqSOclXd6frXhfTUUAf54KvaV/fVn+pj9XOe7XUbcIsaEQJrwDEwiIgLoA80EXEJbAOHwNOHmh7qUP2lxl13q+JBXeShNlnhrNrcr1qoWrde1YWkcZGjtpApYAGb3YmIcbeiDfWD2syRsAo8miO5Ut+pm+XBx+zGetvatiPqJ/VLCch8Jd8MZ9Ky9Anwp6jIzN8DvcxbbwK4BkbATQmq8rtM/RZKzktVcwa4KUFX+beX6veETtuGdSDt2TPgCfCjNG5DPfXhcaY+VyPUyLa+ALZysstK/pvTXH8DBxHxbebroqldNP0AfwH4tpNvbtB3MgAAAABJRU5ErkJggg==",
    "upload-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABJElEQVR42p2SPU7DQBCFv7UN4SYERQihtCgVdDQUFJyBnoojcAdOkCYlLRUcgI4iICUFdEgQgeOPgnG0giQFI1lvZvzmZ98uLDG1WBevNLUK3FVv1UGeX1dYBu6oU3/sPWtQsm5VdVt9VWv1Tf1Q5+rB0iOohZrU/SA/q8fhn6n3sUU/eAXAoktKSaACboABcAdsAY/AITAEyuD9mfxb4W5MO1rFrWJqEz9S5OZADQjUkd8AvlruYm11oHZjpSYIFZCAKsur9lrx2lXG6jD8zcC9ULsfcSfwWn1ZrA1MgCIeQosPQA+YRJwCp8BnXtwBZimlOs7a2lPm15kuZV48Bk7UK6AJoVpNWoFSxOdxnVTR6QKYAaeZUCm/ofgaYARcRt3/7RvqWQOge7VBHgAAAABJRU5ErkJggg==",
    "video@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACfUlEQVR42q1Uz0tUURT+zr33zZth1DRqERa1CNsUpaOGGYhoFu5avBYzWv0V4c5W/gMtWtWiGhc9WgQRLoIS/DHKpCQkFFYSuSptnIx58+bee1o4jppoP/BbnXs55zsfh+8cYJ9AG8EgBgVwe9fEec8n3/csQLxrEoPpbzuvN9yMB8Fim6Jk4/hxJVwFBDuKiwDioqZuOVxeePqmMwcweR6E75MpSyECmJKJ8SFX1Q4w2zIz7dCsRAyBzi0itJcfzp1/DwCp1sxRWB1PZy++o2RTJuGqeDY0P+8y8zRIS7AyFQq2JCFghDYK0TuGw6nhmfaevkTmliBnyHAx0CXdpFiaQwCYGfeGZ9pe7zWfVNNYtxTRq33NmTlHxs+EpfyolG6HdeikEoAGQCRNbUfHSxWLFWShEDNbCWJL9bJQv2RoTbquqq4q6rwolnLNRJIluVkjTFExE4EAWGFGRzu15z3mkZHebUSexzziN5hUYiJS0Ctf09m20wCQapnqAkDMhsS/mI4BISkSuX52oh4A2JZsxZCplvGuCNW8CDnfHZ5YegVAAtim6MdslapuXNORj8cexJyDyaJeLRnWNwXxohLVY6Fd61EWUABYQK36/jXzO0kZBgtAMjFWDEq5FWb7LOYcTAf6+ydrQyJmV5GR3+AQWWP6U4nJKsNWSLJ2k0MBACyHrqLYFW2D6eGZ9hup5sknkpz7hksrYUnPVwwZVXUDVDHiTkMSSYR69TOHfCk41frB98l4554fdqn2wKPZCwuViv6WqQawcwQIYNlU/gVJBhQcioZF+vI2Pd2bX18RX5RHAWzs6v8uLYNp473ljLCY9/w9Cf94RvYDvwDzNSx4Sz/+9QAAAABJRU5ErkJggg==",
    "video@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACBUlEQVR42o2Tv2uTURSG33PvTfI1pqC2CiWgk+KkklYqRQkdHBwVP5UEp0hdXNTNJcQ/IOjkWqhGaMEO4iRYxFCV2grFpbQdFIogRiH50ubH/e7rEG0zpNpnP4f38D5HsIOgJwQgxG74/rTGP8gjrwB2LadgL+TTNN1DeVB18lAkn6dafTH/wCjvpmVTBJC/mwlxWkwclNcWQe7Z4vgPALhx5tXA1MKFimRT85e09p4713xIsgmIAA6AkKQC0DbauxWyPtnaOng/Fg8eafGuWVv3DcAjNmw0Sktjd3aLnkmVB43el4vF69dBCYh2lOBlA6ANoc6ce3vgmLa1b0G/DCVqBICfG0m9nNwIJVCHtcQiobPFp4ujxUyq/BWiQ8M/FXlObKE8bjv3dqrxffLNzPEwOzIfbdrqWmlprNgpVTwBaQQgATQUTTo9Z4JgURKJOQJA7dOqTqfnBAFq8ejgqezIh9mWrUwIuElCGQARUMJS+fyvHudarAGZVHmzZavfhTwZ04fWRSnPhQ0YEXwxus/LDr+7C7BKioiQAEBSQai0eFesa06uyOd7J+T0Y43oRQKz4vvTOrKeLBjt3Q5dqx9w0qkLAOCURJyALynMTS2MVgDAPzvdN/P+6ta2ZhPDHyM2dtQDVrpSD6AV7HdPlofqO4ZBChC3rej/3WZPt/coe++v+g1KN+a7+WcD4QAAAABJRU5ErkJggg==",
    "volume-2@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC70lEQVR42p1US2iUZxQ9937fP49MfaQGq2hACwXjO0mtSJTJgKigm0h/NWYWvrpyVWo3hfIzdOVj2YUgCOIDnAEFsV1YYfK3mmBtGy3GgEqhomhw1ATnkf/1XReTpOki0fbuPjjc755z7rnANOVA2LZF2XZe4f+WA4dnRgil00UNCE0LqQOA7rW9nxxYP3gp29a/b2LCmVozANh2XjkQTqeL2nUz4e7Way1Kpfo1N3QZyKmeVcXFOZAZp0nd7b817Vt351hPu7uwPpXDGgAKhV0RAMCF2d3a25K0Gn8JjTdS9p5cI9I7Nc8tT+UFeE2Wnvu171VWALTdtvNMjiP88Grfl5qTn4dRjbVKfEZQr6p+abWlG3oUqaNWLJh9um/jmwnqrpsJ97S5h2fHm7+v+sNbzv2+4Sd+/MPtJZoTJyLjNxMrQOi8b553FO5ufkqCD0UMokqgAGBve9/ZxeVk0UkXdTQ271QtKL02MN8CgPbhWwopIyb86sLAxotTnXsEBFMFJeDXBmte9q/Ki47C/ZVud9vPl5msbHb1nRQzGSGAFSjlQHjbtgdxZ3k+lkPOyLi9YTxpACCF2MlaUPKMyFYAxGwVLZWKUbzcoicVZKLedC+jBqoLOs0GASKCeP0VgUAQo5Q2wsQEI5GMum4mBBC6k9RIAMCPNAFAWbz9SasxUY1GrgAQY0zGl7JPQfKeFkggEAbzN9lP+3cyxWteWDqRG9gy1IOtFgDERkMBACIcHgtH3fDjDTfs6r2Y5mpXJMGt83+urXCw9NnfoakdUSo2x4h0EuhAQs+/Ya+5vkgIr4gY8fn1RuKbTr82tqNQoMhKvPwiaTU1Mvi7cSP+XVMX0hjvNpHu0uHYgjN3MyMTmO72m8s+iH00VPGGf7zwR8d2286ryYhMhPHiQOdQLRjeRMCcWYnmPSKmHIkf/IMTAuKlICwfJwoPAUKFwqDMGNqD6wevZltv7n+f0E57i/7rGaEZmzkAckAOZN71+VvYi1oUkIXCjQAAAABJRU5ErkJggg==",
    "volume-2@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACY0lEQVR42o1TXUhTYRh+3u+cnZ3JlqJUCEWkqwsx2pyGaaTSr9RFF43S5SQqb6K/y+pi7qbrukyISMlBN9KFqBnIDDajNSZBhZMiIqTCfrbl2tk539vF7AfS6L15X3h4f3ie9wH+CiasGkwhsFgRCiG0DIRE0Pes9l9DfhVtbVNqny9hA4DDvptlAd/M5OnmNAcaYhcBoIQxBXyJar/3vvtnnwoQR6MwowD8dfc0h6gdBajdMDNZJt4J4Hq1M8sAMSPW77TVBIPbY+7Ns1hQT3qSaw21eJmAjczw2JU17pzx4SARrgKkAcBczlUR2DFjwDBuQBFHTVVeCYPOioIoHNGV8ktgdiukvswU3u6NJFsnCCgnBgGAYGOYLTlxN7X7eb7wPihg6+vyxLeozGzPFxfzw8lW72/iWMxz3PpFD8tbqtAj3Y2PapzseLAEUyFFdgpBLEFQe5omq8655+y9ba/1MEiWKCllp9DHATLIovqBp41FCSvPEpWqZBIEaQ492bcIAJhf3ow4iEuafmPjgCBNk2y+6PMlbEuwdBbWoiBiQ7dVOgK++EhPU3LomHd6TxgkQaz8oesZi43HkVR7OmNm9gOQ0qJx1S4dI4aVqSdSWlgWN7js608c9053MvAVBAYASfYuoZhGwDNdp2lVgwXr80AktSut3k41fARwHgB6N03pxjp1TFOdY8wyK9kYBYCtzuyXcLTD7PbFLgCyTDXFtRBYCIDJ72fF72flzpuO759k4pAljYe6rcLFoDgALORcBDARtP5c8dW2wdmWd2GUrlrxt081p92rmmB18/y/q34Avz4TKcQZVHoAAAAASUVORK5CYII=",
    "wand-sparkles@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADsElEQVR42o2UW2hcVRSG/73PmcyZS3OtIU1aLa2CjDROciYwDTU3KTbppChycjnTRKhEoa3PPpVhHkRqXwoWISJIbJOmMxU0jmlfmslo00aYQ4zaiI2tIoloNWknmc71nLN8yNVQqutprw3741/7/1nAY6qn7hvPppYFEOBrzbG6Ww3qvkgJABCIsc0PAyA+o4SZK6zQrDx50io4z+T0pbO5TNGpe0/8ZcZizbpP7rcXM3fYbtnelskv/EZG6tD5qaYf2aOU+OQRexGV3XZYK6qSuT/ypkm7h6cO/O6T++2lXB4tEJyNyey9ty2C7TiRwQXdWs0BQFFCQiBA3O+edB2tniiXtfYMOL2oG6nrxNjBFciIvYTJV6xiUWMyv6AOTzW8lzdSH0mW0l1UkKxiADGAEQCo8kQSYANDWv2JrQpLeOVnFia98ND4u/eS1hTudI+rTmvFYFpfuLqYSLzMAOBo7USLKDjeyZupWsaEFJExbiLbNxRvWuh4LuyQ7Ls/3ybtakmkf/1+UNtf3V1z/VWnVHE5qyfGF835wxHtSIoDAOO8GIyXEiELMjmAnXaL1Wz3fGiT7Hu+FLnUcj/18/uMCc/45RvTFlG6nNUTsbRwpy2iHUmtuknrH67KE4Yq3zgPAI1PfSz1erRrfd5Z6pK/6gaArprY2RMH/iS/Z3JScYWca04DAAcYBUB8JQvUCiYGDlZ/4niyvPqKVSxsWc7MqcNaw8VOeVy1WcpOLmfmxiibbw3PdCQVJSQEwUxszdDqifd44mN93lnqckdXlLij3X3eWerxxGOKK+RUXD84u6rHawBiihISAICtQYJgpuIKOSXHni8koahpOTOnDn/bfLHLHe3eJu0cyupLY4sUb49ob6b8tTePW0TbOcr+Ujow/cqDAIizAAI8iKCpeEM2m743sj7OJkhGT0Tvk+arhJx/iFy/wApaTDKrRG6d0Sn17oW49xIPImj65BG7zdg7+ihIVl8ay4h3D0e0N9IALAAqCSQCZh6MFYFoO8CIv/X0bWshyj61cEfTUmbevxWSFu/4wpMd6QDAdvjkzKBWf8g0c2ckS4ktb8zvv6DVnwuAOLrliWdf9/5Ene7oKQDolL9W+7yz1OvRrinekO3fRhAjEPPL8R2q52YrgdY3Anvt+WixIdq+Y0ww80b6A6e1/HTOSMbSwt3WFSUrRuA/ig9MNz9gZuYlBmKFUtXpjJ4YXTS1tsdDNmzfuFpNtrovUnKs7lbDRq42ltj/qX8AwknDCDTabNQAAAAASUVORK5CYII=",
    "wand-sparkles@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACt0lEQVR42oWSX0hTcRTHv7977/6ZQ8Ugk0IqjaiX3E8tXXPYQ0RUbyu36zSCFMOmaE/1MKTHFJoQUtFDzUwa0oNZPRRL3abmJlq9JGJakpGYmrrt3u3u18PU9hB03n6H3+ec7znfA6SExfKMBwB78ehBKx2uT2YZ2cqLBp+ppihkd8LJAQD5izICEGal3p0cNME09a68sLzQkh8y3mkFSdio36Hm9S4Vn4aIvNgvRaKVQmrHrJkgJ8XX4jGeTcnKei4h/Nwm2JKlO9C2Epm9L0lLr/Rpec8T+HGRM5u9gsdzQRFmfDc2IL95NFmxEotmnI1Jv0q6gqW9Nup36FTZbWvRbw+ehI7XCSr9KVlZAw/ymQCMiHT4Ns9pbEpCygFIP5Pj1d0fTctWOtis1+xtD0uL0bD884hWnX0zS7fv8tL6VGPPRHmHAAAswXjCES0hAkmwuC52aOG3Te136DV72tel768ZEgXpmpwPakG/Y3ljurlnorzDYmE8zGavAAA2g88pGgJ+ALAWDjXUG+eZvXjMDQD2ovcvaku/sCoaaACwzWxbdDr/pSZpx1BTvXGe2ekmSMfcdWVzzEp9zQBgOfxJvbVgpFay0s2O2+Cou944z0Tqu5YsHBiwGny3thhisTDe4yGKjfodGdo813p0ocsdKrbb6Zg7XZtbtRqZaSg4d6Jzum+4jxFykufUkqJEH3aPl10XPB6iVB4ddGTq9rvWIl+73KFjm+DuqtXobGP3uOmunR/NZgQCB6IigAIGDkkpI6ZLJZOsqijYCQDVdOxxqtRaGlRtjSfSwFsxRTZEGqi5UjrNKgvfnRfpyL2rxgVmo34HADi3t5q8b7PZK7CUWyc1eV5tLFvdq1VlnlESMmQl3PR03Ohymr1C60BFHP8LJxhXXTJeLRp8pqR1jP/3T0ZSX38AcFc7xlKeqnkAAAAASUVORK5CYII=",
    "zoom-in@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADWUlEQVR42o1UXWgcVRT+zr2zs7tusnapYvNQTFtBiwRkZ2sxNi7aUiRiC5UpNrsxihJEEZGitIiM8yK25sVKXwI+VJqAuw/6UPAXNqGkSZtO1CL1QYMklYqV/NQ1szs7c+/xYV1MQgJ+j/ccvnvOx/k+YB1suySxCWybJcC0UW3NowMWLkgDwMBez2Id7dJaJEjQH1qvXBvx8r83+xzhwtUbErWKz1oTT8UoMRQ32h9QOgCIQBBQOoDi8KPAX3y3fP3JxfVk1Bq5XCbVl710si3e8V4t/POiUo2zEvJaJDQL1luIYr1SJt4B61lfrzxR9nrm15Dl8xUDAPqyFwdf7p7nYu7Kmc00OmqNZ5/f8/3tfmt6dtC6Gmvq1dSMACbb8tJJIX7SOpw+7+09PJCvJIBO6OrNPER8QITylbihGsNezj9qfZttMzq8erT8+qj36JnWNgIgNhH0JmKZDo3gNMB0bvzx+rnxHfWIxM6Uefcxxb457OV8BlPJOzBTj5YuAzgBAOUyNAASAKCB+/3GLUaDrgPEBWvq4AsP//Cq0Hiw1liM2KCBojX9xnOWt92BIwBRkWTe07/nm60AMcAwAICYDIYiSsZUUw39kmncaSsdImSfTZk6bcgk6sHCLy7cGwV9MDBicUHhHW0AFpzWRCBeMo12cKgyDljE/9Yv1oLqfUo33jJFGzNWDvjBb5270sEXAJgFdzZUNTBE/CYAuCBuErEekxSHQuNpF6SrGRF94nXNAtwwZEIEovbj6Hf75zA+pgFAUOwwQGPDXi50wAIAC9tmOTrT4/nhrc+T5l2n+qyxrvJUd822S1KQ/qweLBxS1W1LzZ9dXbQun5UikVKROr76oOlfRvxqTewgmf4arDoUakfOT3d/ufqG+rouZMjc+uGW5M7+Zf/niZGZfftsuyTLZVsD1DKgIwBXH9l94d5UatvHcaN9fxAtz2lWFc28JIgeEhR7THMUMeuvkrHMoXp0e2jEe+RNx2HhusS0kWGLuclnCOYgc7gbhDRA8wJyLMDKB59ezd8oWFND6cT243/V5k6NzHSfaG2F1cbldTHhOGubWpYqZC+9/1rPIhesybexee6U5OpccuCIfL5iNI+RqVUr5q6cPGZN9uL/YeMga5L+h38A5tOWhi63jwoAAAAASUVORK5CYII=",
    "zoom-in@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACf0lEQVR42oVTXUiTYRg9z/ttbdrWLCqoFCqimy5kflM2FTfvDKOLYNbmFBELoiAolO4+gi4iQ0TwRrC6ULHtOiW7sJUbzj8aoZVWZAhhRFro3Ob3fU8X+2kE0bl7XzjnPe9zzgPkwaRAEfgHvN6g9Pcd5YgAMQD45el6SUgtuqaeISHSgBRjNfFw+HXdkgIWd0B6AZlJAejHqXHjZsnBR2bDfl9i9/tHZn2RAANIqjYb9pUkdze7hhequ73eoBQKNWkZO8jYCThiI21V8VSrY+ZsobUr8pyxRZ692eFcZp/88mrhFwgAAvZow96io+M/U6vnRufdT73eoGT8VNoHEvGROecAAPgrIn1Gqfi6QSSPDMZc64AiBADohI7t1Ne10fm6MQAIhZo0AhrBWmPOgaaJHiKBnV3tPAC43Z4MGcQndOizmYFF+9sq44tgtgLkbKt6884vR/1P4q7PafXXBkC1OcFsNKQRwwwQC+I4gLcMUok5BehxAKsKWBAJCYRtANjaWiZDJilekISpueHUmGlormYAwECzHFlj0MzjmfKLAHDJPumw7imzJVLfngOAxXKas7ap32iwWg7YSm78aQDfZlAvAChQhMlou5tMb7wntXgCYAqHPVo+Kl/FVOdl1woH5Fhva3n0WE6jxRGxB+TYs2u16+yrmBrMDGvSkI8qF3yzPdIuSaZ7ROKQqu+sgIXZbLSVJdXNZZ3VKauptD2RXu8emnd1KWBBuRdy1fM6g0XF2skLmq5WgsQOMb36YtmaCIfr1YAcu28rOt65kfjQM7JQc+u/5c+LZ602y9MP/HI0WLAYhWByu19Inuxp6bCHQyHSATCDibILBAC/AQarBY+M52P2AAAAAElFTkSuQmCC",
    "zoom-in-grey@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADf0lEQVR42oVUS2yUVRT+7v0f8+zLYgmwsCrGsjAa3RoVNS4w1gTTPyENNEUyjZrgYqSUvu7c2hdjhSBg4oKiNO3ib4zGha9NmxAWLiCRGHcsqKZQWjrttDPzz/z3v8dFZ5Kh1vjtTu453z0553wfsA2u6xr4D7iuaxAR2+nNrA6EENxxnAAALoy5LwVaPU2chznDUk75tx3HuVfJk1Lq6lpWTSKl1OPy6tu2HZmIRmItvu8DDOCcw/d9BIG6mF1eTMnzydXtZKzSsuM4wfjQtTONjzWNrmdXr5dK/mXOcdsMNCmg3jCtQ7YdGtCk7xQL3us9smOhmowJMWdKeVCNia8Tu3fv+yqztnIx2Xvk5E5zGBaTL8ajNXOBUivxXdmWRCKhAIAxRmYq9VoQibh1YVAqk3nwQ7Kv/aQQV8PNAJYs+1XL4B3c8z6MorHUJVtvDYvJg/W1DTezy8EHjLEvyssJOGOMmF88FIvV7QlUKU1ETMpOr1N2epzwVH1N4xEFw+6SrXmAWL88fmszl/1Na90DAI7jaACssrVn19cfUmDbfzLGKP3p1FuhUPQZz8sf2NhcU9pgHefGZlTJu/ZtAeJvBjZnmVb36Og3jb29HQ+JiPEtHjI1aeZFdbAV4kQsGrtkmeZHxZJnhEPhdCRSc87g5vNSSgJjRdOyuRUYcQBIpVJlIuKZSDiGeJY1CCE4hYrv5/L5/b5S3ZFwlHyl3lxZXmre1Ht/AkAgai54+WJtU24RAKSUxAHAAJu3LAu+Kr0jpdTm/XX1cffhOwBKth3iRWz+MTBy4i4wrwHAMMx3GTDf1dXlCyE4AOKu6xqfDLbfXFtf/b4mXnd2qH/yueT5ZMF1XYMC/d3GZqa1VKrLlH/WEyMzly3TjmkKktUHzcqMiJktT4ZD9q+k9Z6iLhzu7j32c/UNjfV82WDF6y40Ne07eu/+wo3Tg8debmtzjdlZRwPYEmDlQvv6Lj3RGH/8SjQSfSOX27irdDBH0BkO/oJhmK8EOlAg/UssVtuaz+cmTvW3nyrX0r+0BgDp4en3LMNMKO0fAKGWMb7AGJvPB4XPBgaO/5Uenppo2rU3+WB58Wz3wNEeIYhju/q32wTRo0lCCBMA0iPT49NXrlN6aKrvEfXv5EkVSynPkQPQUkpqa2vjs7OzweejM2eU8n8/PdjxI/4fOxtZZUkV/APEH7nHUsGq5AAAAABJRU5ErkJggg==",
    "zoom-in-grey@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACqklEQVR42oWTTUhUYRSG3/PdmcbG/5lULFy0aZEUFQQVRtYiCqOV90alYRZFkGiJo+TkN45mo6P9SEIaWJoZ3CFo0Q/Voha1qECQyMoIDCKJDMPmx3Hm3tNi1IYgejcHzuF85zmH9wP+iKSUAv+QruvK3zlKigwAXeeGthOUcsM0CiHEnEL0MhIN9zc2HxmTUorm5mZzsZmZyePxkMPhsMbDudfT7On7Z379/MRsvgVgIVK22O2pWeHgL1ddU7lfVXUlENAMAICqJnD8rUPD3f470Yu+27uT0XqP9Vo7226dvnrpHvs8N04k9xAAdLQM7nJk5z2c+vF1T4M8fF9XdWVi7Ww3kzJa7z7YBwDt3qFum23JScTj+TWNB75JKYUAAGY6Ov1z6kt9U8UDANACmgFGCTGXLBCYRuyCIAWzZmxvIlMsLInxvNI0jddEBF/LYI/dZi+ORCPpAG/q9t95PxuJeF1NZcM+78A0kSgCcA0ALPO3NsCcAoCJMMrEeWAsIyBqMo+C8TmBSQozQgAwOTlO89gYsVptW6uqLtvq3Yf6quvUUgJiDLyqcZXuc8myF1YUbMjMdGaSwBMAyM9fxZbEeNGzNCX1eEGusxpAx/yaDQqJCQCQUoo0e0ZrMDTzgWz8mJmJiAyLqupKbZP2pt0z4MrKdnZ0tQ0vhznnr3WXDwFAp3dgPRSLz5Gdu/Pr5ER/w5mKUDD4yQIgTgCgq7qiBTSj3TtQaV1i8xFETiwW/QhCSqo9oyAUmhk3TeO505FXOT3zw+9qLHNJyUIBgMBYgKWUwu2pGVm9ZvOVDHv6OwaHFaGMxOPG+WBsvPas59TdbUUlqXk5K+o2bdyR4fase/Rf8y9IyqeWhKFudnZ4B/Xkj7GohNefLT5SWPidNU0zEyUmIuKF2m/Bzx8bI/weoAAAAABJRU5ErkJggg==",
    "zoom-in-dark@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADLElEQVR42o1UTWhcZRQ9935vfvOjqZMJ6cJE25LJTAa0AVEQIyguUmxs7UtSQYIoWQjiQi0WF+PgRks3VkQUXdSdfdp0EVDrou3Chcp0IZPpLOxiaGiaZGrayvy+9+51MTMlHVLxLu893/nu37lAl9mwDe5j7RjtFOtyZhjICgCkh1+ZVHH3qFAYwLqv7p/F8tJaN24nIgYgiZh9wDCftDiYUEgbRBD1ISqfaX3rw8Kd83938Nsfd1KW5OCR4+FAzzKgm02tz/tNLylubbwhladE/I+Y+C2O7Po9OWA/3Cbhu2lMYcoCgGT88OLjw69pKj536n49SsQO7U8PHb2dGpq7OonFQLsi6pRGkwN2fyNorijkj5X1MzMjIwthAOit1qaIacHU629aUb+ZW1uuJmKH9getaE685tv5snPKhm0cOD4D0FoA0xaHhyE4AYBKpdP1Uul0HdBHAxw9quG+YG5tuQqAiuWly57f+E1Z3wcAB44AIAsASGXM9avKTasAQFODL7/AHNon6o27ft1T9RbS8XnPN/4PhbXkKmnhAhEfS+x+6aHi9XM37xIp2AKUIob89jDfMBy0VXyousocOMFkQUT+ArLXhOyGxYZJor0AbgIZanWddMviIP4J6gCQYU+ar7teba+KHGMKKlSeb6IyGkus/whAmXRUfLcRvdF7vfVxVhkAROgikQH59ReBrFQiIW9l87urIDSZDCsof+XG2dKlS8+294ZnlPRiDl+5reWEsg3bFMtOzvWq5wyFPxmLz6RXV52aDduowZKnjYOy4W+1f5aJ+NznTKZHXX1n+0JTmxGpweIjzOa8qg6LeocLm85P23co/cCBAQ31fBqy+l+tebd+LWx8/zRgG7SmprRdHuMPTo+YUP83hgLPedIoAXoBqlsKeoyJn1GIB8XPlgkd9KV5Mr9x5r0MMpxFVjtKVyDD5frXtzYq+W9jkUSemEcJ+gSInySQCyIHVmN+Zf3sl7HIWF8o0PfursieiFP54pdOVV3q13suQqYL1JFUKj778eTuRU3F7A/wX3en6y5xiyDDrb62YhNDs8cnYrPT+J9GO7vvzfRfLuxlT5K12eAAAAAASUVORK5CYII=",
    "zoom-in-dark@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAACZUlEQVR42oWTT4hNcRTHP+f+7pv7xhuj4b47KAtRrzePkVhYqJcVIoquqRELSSmlTGT3UoqQJDOLWUgp/66FFMJKsfCnqcl4BgtGs5h5M9iYOz1v7j0W712GkrP59Tt1vud7zvd74HcIlCz+ET6++Tsns14FKLTv2oDKHtAC8AOV5xrFl8tfg3Id/EScFJtGR1lOc9Pi9jVXbUmfi7TWKspHRGuIbE/ZTo/bnAsnwr5nPr4pU9aEkGl0vNbZ3l3Nu12bZ1Nbw4HUCq/ryKqFezWf3XFw9ggC0JH1Nzl25sF0bWrr8GRwz8c3bzwuAoPlStAPUHD9i5axD6nqoqHKjXHAqi9IdH81mhodngzuAwQEkShbRNmSMJgx1nlBiKltAyhSbBQjS1FeAnR4fu9Kr/sNwlyFdZ3t3cMdnt/9bvzmpyiufgNrfQKYSBOBphsbH0R4q8oMQlVFB0VlBEqWYhlgCuA738Wus2ZALHv3cjY5jRn7C54/quiL12PXuwByC3aubTKZeTPx9GOAFlrUArCEXiOpFifbejihFMNxVC7UfyUrZVIna3H1nSXOI0Ce8CQy4JvKVDDmNufClGk+42ZybYucjtdDk8GzibD8Oe/uXL2w5csVx567sRZP3x2q3LhdpGiPMBJLoltAEOVdf5+xzGkRsrFGHxTSKctZMhNX36vq0yY7s+9HFJ4tV4JjULIMQN0xJWsy7BtoastfSkfmrYqGggyoRKfc8UrPq/DBHXdOLuPY847OTy9rnQj7Hv7X/EkUKdoAhax/ruD5t2Yfxh/HUqT4C8TD04AgrsuoApL4mp/Es+tbErhd6gAAAABJRU5ErkJggg==",
    "chevron-up-dark@11": "iVBORw0KGgoAAAANSUhEUgAAAAsAAAALCAYAAACprHcmAAAA1klEQVR42mNgoBAwhjKEMjMwMDASVEiAjymhKxbtJynpwwXh1TPBxJkQCusZGRgYGLTFw5axsXBuFPrLvV1GxoKTgaHxH8wgJoTCxn+6YuGrmBiYI7///pjIwMCgJ/hbYa8xQxorzEBGiDWN/7RFQzeyMLP7/frzzf76m3WHdISDNBhZOE7//f/7LucrQdOzDJJ/mRgYGv8zMDAw/GdguPzz31e362/WHVJR8WC/8nbdjX//vtsw/Gc4wMNw8z/UOegA5iGExzAAJGzRFdQz2TPYs5AVWwABIj4ymk8QPAAAAABJRU5ErkJggg==",
    "chevron-up-grey@11": "iVBORw0KGgoAAAANSUhEUgAAAAsAAAALCAYAAACprHcmAAAA90lEQVR42mNgoBAwrlq1ivk/w39GggqROTg1/P+PkJjcs9avvn4mFwMDA0N9fT0TTBzGYGxoaGBkYGBg6G5dukyAX3gjLxvv9sLCXs7GxsZ/MIOYGBj+M9bX1zM2Njb+62tfsYqVmTXyyfNHiQyMDHqy4rJ7Z86cycrIyMjw//9/Rsb6+v9MjY2M/7qaF23k5ubz+/Txg31lY8Kh1vr5GjzcvKd//vx+l19M0/TZs81/mRobGf5DPMNw+cP7d26VjQmHJk6cyF7dmHjj46fXNgwM/w88e/b5f2Nj4z8Mj8I8hOwxDLBq1Srm+vr/TOga6+vrWciKLQBj/mQt9tn2jQAAAABJRU5ErkJggg==",
    "chevron-down-dark@11": "iVBORw0KGgoAAAANSUhEUgAAAAsAAAALCAYAAACprHcmAAAA3klEQVR42mNgIAeEMoQyMzDUM6GK1jPZM9iz4NEG18CITZaRgYGBQUssNE1NLFiPgYGBQUsrlI2BgYFBVyhQRkckNI+BIZQZqrCeiYGh8Z+WWOhRZgYWg/9/fxpfebvuho6YrzgjA/fJfwx/WH+++qJ0h8H8NyNUA6OKyklWzs8Ce/8z/Nf79+dXABMz6ywGBmamPywspjeeLX7LwMDAxIjklP8yMhac/L/kd7Az89j9/Pv5FiPDd7srrza/hNmO4TEZmVBOPfGoPB0xX3FEKGEHjAT4mBqgYcvIQAkAAKW9Oaa/kC1EAAAAAElFTkSuQmCC",
    "chevron-down-grey@11": "iVBORw0KGgoAAAANSUhEUgAAAAsAAAALCAYAAACprHcmAAABG0lEQVR42mNgIAEwwxirVq1i1tLSYjx48OB/mFh9fT2Tg4MD88GDB/9h1V1fX8/EwMDA8P//f0Z0ORYGhv+MDAyM/zubFqX9+fvzRHVj6qX6+lVsjIyMv+rLZsnw8HEFyauxTQ0LC/vLWF//n6mxkfFfV9Pio+wcnAZfvn42rm5MvNFaOVucW4D/5K9fP1jZX71Xeif07jfzgQMNDAwMDEy8gqpL/v/+6cbCzFLjYON1ko2TcwMTEwsDG8Nvo7z29M8ODg6MjDD3MTIy/i8s7OWUEZHcISwsbvf27ctbb768smtvL3hZX1/P1NjY+A/DY4WFvZxTejfktVbOFmdgYGAIDV3FjDUk/jOghgC2EEHXwlhfv5+FCIX4AQClgWw9+mRz2gAAAABJRU5ErkJggg==",
    "pause-solid-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAAtUlEQVR42u2TMQoCMRBFf3YjCx7F3mpbKwsP4Hm8yt5AEOyttfEqgoj7bBIcY3RXWK38zQzvk88kQ6SB5FIAFMbDOdca7iQhSZF/T3ESoAa2wAaojT8B1sFbJNM/BPlQG+5qjL8yfJcG+cxwZ0lX00ddDD+lh/yLBZSZZVj+dKViqDf+B3UrtzUktabP8bZPUGUmrQwfGT5+90XKUOfAETgAM+NPgX3wlvbMbz5v374rzH3q3QAGcpCMJ1rBHAAAAABJRU5ErkJggg==",
    "play-solid-white@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAABDUlEQVR42q2UoU4DQRRF75utob6ICjQSRVAkJQHdppov4FdICI5vAFWBwmH5AjwCgyOEQJeDmSV3l9DONjwzu3m7Z+/cuW9DVkBICklIShFR6z8KSEDq+1IAQ+AAuAOm1htktWshVV6PaNcC2HNgX9AHUOfrd+AcGHWfLwE1kKWpewLOGlVA9cu/FSCAL+DT7h+AEz+QUpADXeE1sGuxKQZhvab/BsyBSJtGLK9bObi9woakpaQkqZJ0L+kwIm5+fCrYmnvzCJx2zR6sUVGbghdJF5IuI+I1G9yeR1M0seOuTd0VsNMnkMedLd0C+8Uzlwc2gDHwnH2Y+YeKhtZDBWwDw41/I62ErvLhj/oG4zXm82LG/3sAAAAASUVORK5CYII=",
    "play-solid@18": "iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACD0lEQVR42q2UsWtTURTGv3POTZ6JiiAoiPgvlEqiFkFjQVwUF3lFNF1dHF0cxMdDcXLUpaNVh9yhughOJSXSEpK4iqN7h6jpS/revcchjRqrTQp+013u716+850PGBdFUAaUwlAF/0sRIh6CJ4t+HZXulNqFnmJGSB95uOcv23NvAKBSWTX1+iUHkP4LxAAQhjUBSL/qYM6IrBPLZSPBymKp9fbWmcZsvT6fAaSVyqrZEzQuReqTdDvb8iL566LBRrXUfBrOrh0bAkcPTwQBAISIOM2+Oa9pkDfFewdM4WO13LxbqUTG2gUXhjWJEPEk0I6DJAC0n3YzQE/mpPjsVO/q+u3SxhVrF1yM2I9gZpqBEJFxmqpPu95IoQzC+8Vy02baexi35z8BSjxtFAhEIJLUJT71iWfKhQEf7VTLjRsAgfcdLgJIoQoHIi54ZQamDNvONFUVmeGA8+awqGJtK9u8+Lp93kaI2EyJcMxGAnPQbGfJZ6X+4+XW2eVR+mPEnicQHKAamCPCyptpljwYJN9LQ4hSGNYkRux3TY1JFFAF4FUVeXNInB/4zPeX+ug/se0LX4aBVLGWnLVwo7tjII/UEBVIKGdE8sj84B0rxS/a5ebvO2ctub8urUKJQLh5unFC2HSETNd5d/9V59zK6Ac1C097LO0uaHXmw/FrpVZxaKROXSN/uvuzVvZbbD8AdYXxXQgHhNMAAAAASUVORK5CYII=",
    "pause-solid-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAAxklEQVR42q2SQWrDMBBF35e96CLZlJJeJFDIHUrJPXq10Fv4Pl6ki9bO72bciME2hGRASHrSzJ8ZSSSz3QBIGjMDLpLMI0xJtQCH2HaAJdn2HtgAnaSf2VRtf/hqx2BvFfsM1gK0KYMXYKppF/PzVC/wWou2qYyhCvRbMYBSsX+w1AOtsFnnm+yhzl5Yz7LcsAYY01mJTl/y/ex8jgAAfcVKjH7thz0Bx1D/imcqwDuwBU6Svm3r7j+elTWlLWmo+FTeWCv+ATWrXGE7GnEJAAAAAElFTkSuQmCC",
    "play-solid-white@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA2UlEQVR42pWTsQ3CMBBFv4Nbesr0aSmYAVEgsQAT0NMgsQMtGzBAhERFhViBnpI6IvBobDgFEpKTrLNkvft39rdkAuiF7OK+UwCZLQa4f4ALeQmUQA4MzblvgpOQT3ziDmyAQXWsOngPPIHCFLkCi6j+NYqBDwEojXqMMzBuUq7ChE5skR2QAs63eAAnyUsqw5pJ8s65aRsYSY9QwEs6Slq/525o27Z/AebV5/0FF2FWgBuwAvrGfUndhVm1LZA2msV4OirnwMhCtTY19syAScXbSdfP0epXvQCANmIP9lYL9AAAAABJRU5ErkJggg==",
    "play-solid@15": "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAABpElEQVR42pWTv0tbURTHv+feY2KQOGcp7e4QNC/YLc0kFIdCiYjk2T9AcHeKYv+AFjoXBzu0z1WwkzwCTW38gYub4OLmYCOJz9x733F4DUkgbep3OnDv59zvPXwPMKBK5ZtOKqF+/QQtF+sz/WaiAaF/AvLnwsrcj41V78RWvV8H/uyR1zsvlQ55FKcAYBMgACDQG6UmtCZegFYNv9D8VCke5sKwbIe/NQAPePgNxGLdfTcWw8yZtUnJnlULR+ulUo2DYMklDRKnwzBBA0QAaQB4MC0LxLkUT3183l5s+IXG6yBYcgDJiJeHRQSOxUhkWpZIe0pN7vuF5t67fOOFQEiNnz8REdi4e2tcJ8pmnr21Wj4QSHg8LCICxzrNmtJ8F13XlZItQIjHYE4prdOc5a5rX8bovN89nt/pBekvtsUBIumJaQ2oW+PatdZdd3a3Ob8DCNVQUxhpW8SxzqRicXA2+mxse/vrefmqF5YwJLsFCAAwAFwgoCRpQIqndcfcfJc43vxy+vJnH3rlwpDsKJsEAMv5+oxfPF4czHZi8Un6v616BBfVsvHC2KubAAAAAElFTkSuQmCC",
}


# ---- Offline Toolkit UI: the shared look (the same block in every app) ------------------ #
# White rounded cards with soft shadows on a lavender window, rounded fields and buttons,
# a purple-to-blue main button, Lucide line icons, and a purple header (and title bar on
# Windows 11). Tk can't round or shadow a widget itself, so these are images ("9-slice":
# the corners stay as drawn and the middle stretches) set as the widgets' frames.
UI = dict(bg="#f4f1fb", card="#ffffff", tint="#f8f5fe", text="#1c1433", muted="#6e6787", error="#c8364f",
          purple="#5b33c6", purple_dark="#2b1655", blue="#3b6ef5", disabled="#b9b2cf", select="#ece6fd",
          select_text="#3c1f99", border="#e3dcf3")
_UI_CARD_STRIP, _UI_CARD_R = 31, 12


def ui_family():
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for name in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell",
                 "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def ui_image(root, key):
    """One of the look's frames as a PhotoImage (made once per window tree)."""
    root = root._root()
    cache = root.__dict__.setdefault("_ui_images", {})
    if key not in cache:
        cache[key] = tk.PhotoImage(master=root, data=UI_IMAGES[key][1])
    return cache[key]


def ui_icon(root, name, size=15, colour=""):
    """A Lucide icon as a PhotoImage: purple, or "white", "pink", "grey", "dark"."""
    key = f"{name}{'-' + colour if colour else ''}@{size}"
    root = root._root()
    cache = root.__dict__.setdefault("_ui_images", {})
    if key not in cache:
        data = UI_ICONS.get(key) or UI_ICONS.get(f"{name}@{size}")
        if data is None:
            return ""
        cache[key] = tk.PhotoImage(master=root, data=data)
    return cache[key]


def _ui_element(style, name, key, border, states=(), **kw):
    """An image element: `key` normally, other images in other states ((state spec, key) pairs)."""
    root = style.master
    try:
        style.element_create(name, "image", ui_image(root, key),
                             *[(spec, ui_image(root, k)) for spec, k in states], border=border, sticky="nsew", **kw)
    except tk.TclError:
        pass  # already made (a second window)


def set_titlebar(win):
    """Windows 11: the window's own title bar in the header's dark purple, with white text."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        for attr, rgb in ((35, 0x551a2b), (36, 0xffffff)):  # caption color, caption text (0x00BBGGRR)
            value = ctypes.c_int(rgb)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4)
    except Exception:
        pass  # older Windows: the usual title bar


def apply_theme(root):
    """ttk's "clam" theme, given the look's images, colors and fonts."""
    import tkinter.font as tkfont
    t, fam = UI, ui_family()
    root.configure(bg=t["bg"])
    root.option_add("*Background", t["card"])
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=fam, size=10)
        except tk.TclError:
            pass
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=t["card"], foreground=t["text"], font=(fam, 10), bordercolor=t["border"],
                focuscolor=t["purple"], lightcolor=t["card"], darkcolor=t["border"], troughcolor="#ece8f7",
                selectbackground=t["select"], selectforeground=t["select_text"])
    s.map(".", foreground=[("disabled", t["disabled"])])
    s.configure("TFrame", background=t["card"])
    s.configure("App.TFrame", background=t["bg"])
    s.configure("App.TLabel", background=t["bg"], foreground=t["text"])
    s.configure("TLabel", background=t["card"])
    s.map("TLabel", background=[("disabled", t["card"])])

    # Cards: a labeled frame drawn as the card image (its title goes in the tinted strip).
    ml, mt, mr, mb = UI_IMAGES["card"][0]
    _ui_element(s, "Toolkit.Card.border", "card", border=(ml + _UI_CARD_R + 2, mt + _UI_CARD_STRIP + 2,
                                                         mr + _UI_CARD_R + 2, mb + _UI_CARD_R + 2))
    s.layout("TLabelframe", [("Toolkit.Card.border", {"sticky": "nsew"})])
    s.configure("TLabelframe", background=t["bg"], labelmargins=(0, 0, 0, 0))
    s.configure("TLabelframe.Label", background=t["bg"], foreground=t["bg"], font=(fam, 1))  # (the title is ours)
    _ui_element(s, "Toolkit.Sheet.border", "card_plain", border=(ml + _UI_CARD_R + 2, mt + _UI_CARD_R + 2,
                                                                mr + _UI_CARD_R + 2, mb + _UI_CARD_R + 2))
    s.layout("Sheet.TFrame", [("Toolkit.Sheet.border", {"sticky": "nsew", "children": [
        ("Frame.padding", {"sticky": "nsew"})]})])
    s.configure("Sheet.TFrame", background=t["bg"])

    # Fields: entries, dropdowns and spin boxes in a rounded box.
    _ui_element(s, "Toolkit.field", "field", border=(8, 8, 8, 8),
                states=((("disabled",), "field_disabled"), (("focus",), "field_focus"), (("hover",), "field_hover")),
                padding=(8, 4, 8, 4))
    s.layout("TEntry", [("Toolkit.field", {"sticky": "nsew", "children": [
        ("Entry.textarea", {"sticky": "nsew"})]})])
    s.configure("TEntry", foreground=t["text"], fieldbackground="#ffffff", padding=(2, 1))
    s.map("TEntry", foreground=[("disabled", t["disabled"])])
    try:
        s.element_create("Toolkit.downarrow", "image", ui_icon(root, "chevron-down", 15, "dark"),
                         ("disabled", ui_icon(root, "chevron-down", 15, "grey")), sticky="", padding=(2, 0, 6, 0))
        s.element_create("Toolkit.spinup", "image", ui_icon(root, "chevron-up", 11, "dark"),
                         ("disabled", ui_icon(root, "chevron-up", 11, "grey")), sticky="", padding=(0, 0, 2, 0))
        s.element_create("Toolkit.spindown", "image", ui_icon(root, "chevron-down", 11, "dark"),
                         ("disabled", ui_icon(root, "chevron-down", 11, "grey")), sticky="", padding=(0, 0, 2, 0))
    except tk.TclError:
        pass
    s.layout("TCombobox", [("Toolkit.field", {"sticky": "nsew", "children": [
        ("Toolkit.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TCombobox", foreground=t["text"], fieldbackground="#ffffff", background="#ffffff",
                selectbackground="#ffffff", selectforeground=t["text"], padding=(2, 1))
    s.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], foreground=[("disabled", t["disabled"])],
          selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", t["text"])])
    s.layout("TSpinbox", [("Toolkit.field", {"sticky": "nsew", "children": [
        ("null", {"side": "right", "sticky": "ns", "children": [
            ("Toolkit.spinup", {"side": "top", "sticky": "e"}),
            ("Toolkit.spindown", {"side": "bottom", "sticky": "e"})]}),
        ("Spinbox.padding", {"sticky": "nsew", "children": [("Spinbox.textarea", {"sticky": "nsew"})]})]})])
    s.configure("TSpinbox", foreground=t["text"], fieldbackground="#ffffff", padding=(2, 1),
                arrowsize=12)
    s.map("TSpinbox", foreground=[("disabled", t["disabled"])])
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["purple"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", (fam, 10))

    # Buttons: rounded and outlined; the main one a purple-to-blue gradient; "link" ones just text.
    bm = UI_IMAGES["button"][0]
    _ui_element(s, "Toolkit.button", "button", border=(bm + 9, bm + 9, bm + 9, bm + 9),
                states=((("disabled",), "button_disabled"), (("pressed",), "button_pressed"),
                        (("active",), "button_hover")), padding=(bm + 12, bm + 3, bm + 12, bm + 3))
    s.layout("TButton", [("Toolkit.button", {"sticky": "nsew", "children": [
        ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    s.configure("TButton", foreground=t["purple"], background=t["card"], font=(fam, 10, "bold"), padding=0,
                anchor="center")
    s.map("TButton", foreground=[("disabled", t["disabled"])], background=[("active", t["card"])])
    s.configure("App.TButton", background=t["bg"])
    s.map("App.TButton", background=[("active", t["bg"])])
    pm = UI_IMAGES["primary"][0]
    _ui_element(s, "Toolkit.primary", "primary", border=(pm + 11, pm + 11, pm + 11, pm + 11),
                states=((("disabled",), "primary_disabled"), (("pressed",), "primary_pressed"),
                        (("active",), "primary_hover")), padding=(pm + 18, pm + 8, pm + 20, pm + 8))
    for name, bg in (("Primary.TButton", t["bg"]), ("PrimaryCard.TButton", t["card"])):
        s.layout(name, [("Toolkit.primary", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure(name, foreground="#ffffff", background=bg, font=(fam, 12, "bold"), padding=0)
        s.map(name, foreground=[("disabled", "#ffffff")], background=[("active", bg)])
    s.layout("Text.TButton", [("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})])
    s.configure("Text.TButton", foreground=t["purple"], background=t["card"], font=(fam, 10, "bold"), padding=(8, 5))
    s.map("Text.TButton", foreground=[("disabled", t["disabled"]), ("pressed", t["purple_dark"]),
                                      ("active", t["blue"])], background=[("active", t["card"])])
    for name, bg in (("Link.TButton", t["bg"]), ("LinkCard.TButton", t["card"])):
        s.layout(name, [("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})])
        s.configure(name, foreground=t["muted"], background=bg, font=(fam, 10, "bold"), padding=(10, 6))
        s.map(name, foreground=[("disabled", t["disabled"]), ("active", t["purple"])], background=[("active", bg)])

    # The progress bar: rounded, with a gradient.
    _ui_element(s, "Toolkit.trough", "trough", border=(6, 6, 6, 6))
    _ui_element(s, "Toolkit.pbar", "bar", border=(6, 6, 6, 6))
    s.layout("Horizontal.TProgressbar", [("Toolkit.trough", {"sticky": "nsew", "children": [
        ("Toolkit.pbar", {"side": "left", "sticky": "ns"})]})])
    s.configure("Horizontal.TProgressbar", thickness=12, background=t["bg"])

    # Checkboxes and radio buttons: drawn ticks and dots.
    for kind, widget in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = f"Toolkit.{kind}.indicator"
        try:
            s.element_create(name, "image", ui_image(root, f"{kind}_off"),
                             ("disabled", "selected", ui_image(root, f"{kind}_on_disabled")),
                             ("disabled", ui_image(root, f"{kind}_off_disabled")),
                             ("selected", ui_image(root, f"{kind}_on")), padding=(0, 0, 8, 0), sticky="")
        except tk.TclError:
            pass
        s.layout(f"T{widget}", [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})])
        s.configure(f"T{widget}", background=t["card"], foreground=t["text"], padding=(0, 2))
        s.map(f"T{widget}", background=[("active", t["card"])], foreground=[("disabled", t["disabled"])])

    # Lists and tables.
    s.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=t["text"], rowheight=28,
                font=(fam, 10), bordercolor=t["border"], borderwidth=1)
    s.map("Treeview", background=[("selected", t["select"])], foreground=[("selected", t["select_text"])])
    s.configure("Treeview.Heading", background="#f6f3fd", foreground=t["muted"], font=(fam, 10, "bold"),
                relief="flat", borderwidth=0, padding=(8, 6))
    s.map("Treeview.Heading", background=[("active", "#efeafb")])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background="#ddd6ee", troughcolor="#f6f3fd", bordercolor="#f6f3fd",
                    arrowcolor=t["muted"], lightcolor="#ddd6ee", darkcolor="#ddd6ee", gripcount=0, arrowsize=12)
    _ui_element(s, "Toolkit.scale.trough", "scale_track", border=(3, 3, 3, 3))
    try:
        s.element_create("Toolkit.scale.slider", "image", ui_image(root, "scale_knob"), sticky="")
    except tk.TclError:
        pass
    s.layout("Horizontal.TScale", [("Toolkit.scale.trough", {"sticky": "ew", "children": [
        ("Toolkit.scale.slider", {"side": "left", "sticky": ""})]})])
    s.configure("Horizontal.TScale", background=t["card"])

    # Every window's title bar purple on Windows 11.
    set_titlebar(root)
    root.bind_class("Toplevel", "<Map>", lambda e: set_titlebar(e.widget) if e.widget is e.widget.winfo_toplevel()
                    else None, add="+")
    return fam


def section(parent, text, icon, padding=4):
    """A card with a title strip: its icon and "N. Title" (a ttk.Labelframe, so the app lays it
    out and fills it exactly as before)."""
    t, fam = UI, ui_family()
    ml, mt, mr, mb = UI_IMAGES["card"][0]
    head = tk.Frame(parent, bg=t["tint"], bd=0)
    img = ui_icon(parent, icon, 18)
    if img:
        tk.Label(head, image=img, bg=t["tint"], bd=0).pack(side="left", padx=(0, 9))
    tk.Label(head, text=text, bg=t["tint"], fg=t["purple"], font=(fam, 11, "bold"), bd=0).pack(side="left")
    if isinstance(padding, int):
        padding = (padding,) * 4
    elif len(padding) == 2:
        padding = (padding[0], padding[1], padding[0], padding[1])
    pad = (ml + 12 + padding[0], mt + _UI_CARD_STRIP + 5 + padding[1], mr + 12 + padding[2], mb + 3 + padding[3])
    lf = ttk.Labelframe(parent, padding=pad)
    # In the card's tinted strip. (Placing is relative to the frame's inside, past its padding.)
    head.place(in_=lf, x=ml + 14 - pad[0], y=mt + 2 - pad[1], height=_UI_CARD_STRIP - 4)
    head.lift(lf)
    return lf


def _ui_hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _primary_image(master, w, h, bg, state):
    """The main button as an opaque image the exact size it's shown: a purple-to-blue rounded
    gradient with a soft glow under it, blended into the background color (a gradient can't be
    tiled, and Tk images have no partial transparency)."""
    import math
    a, b = {"normal": ((0x6a, 0x3c, 0xe0), (0x3b, 0x6e, 0xf5)), "hover": ((0x7a, 0x4c, 0xee), (0x4b, 0x7e, 0xff)),
            "pressed": ((0x58, 0x2e, 0xc8), (0x2f, 0x5c, 0xdc)),
            "disabled": ((0xcf, 0xc8, 0xe6), (0xc6, 0xcf, 0xea))}[state]
    bgc = _ui_hex(bg)
    glow = (0x5a, 0x3c, 0xdc)
    m, r = 6, 10  # the glow's room, the corner radius
    x0, y0, x1, y1 = m, m - 3, w - m, h - m - 3  # the button (its glow falls a little below it)

    def dist(px, py):  # from the rounded rectangle's edge (negative inside)
        cx = min(max(px, x0 + r), x1 - r)
        cy = min(max(py, y0 + r), y1 - r)
        return math.hypot(px - cx, py - cy) - r
    img = tk.PhotoImage(master=master, width=w, height=h)
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            px, py = x + 0.5, y + 0.5
            cover = max(0.0, min(1.0, 0.5 - dist(px, py)))
            ga = 0.0 if state == "disabled" else 0.30 * math.exp(-max(dist(px, py - 3), 0) ** 2 / 16.0)
            base = [bgc[i] * (1 - ga) + glow[i] * ga for i in range(3)]
            k = min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))
            col = [a[i] + (b[i] - a[i]) * k for i in range(3)]
            row.append("#%02x%02x%02x" % tuple(round(col[i] * cover + base[i] * (1 - cover)) for i in range(3)))
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


class PillButton(tk.Canvas):
    """The app's main action: a purple-to-blue button with a white icon. Takes .config(state=...)
    and .config(text=...) like a ttk.Button, so the app drives it the same way."""
    _cache = {}

    def __init__(self, parent, text, command, icon="play-solid", on_card=False, bg=None, height=46, padx=22, font=None,
                 **_):
        self._bg = bg or (UI["card"] if on_card else UI["bg"])
        self._font = font or (ui_family(), 12, "bold")
        self._icon = ui_icon(parent, icon, 18, "white") if icon else None
        self._text, self._command, self._padx, self._h = text, command, padx, height
        self._state, self._hover, self._down = "normal", False, False
        super().__init__(parent, height=height, bg=self._bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _text_width(self):
        import tkinter.font as tkfont
        return tkfont.Font(font=self._font).measure(self._text)

    def _draw(self):
        tw = self._text_width()
        w, h = tw + 2 * self._padx + (26 if self._icon else 0) + 12, self._h
        tk.Canvas.configure(self, width=w)
        look = ("disabled" if self._state == "disabled" else "pressed" if self._down
                else "hover" if self._hover else "normal")
        key = (w, h, self._bg, look)
        if key not in PillButton._cache:
            PillButton._cache[key] = _primary_image(self, w, h, self._bg, look)
        self._img = PillButton._cache[key]
        self.delete("all")
        self.create_image(0, 0, image=self._img, anchor="nw")
        x = (w - tw - (26 if self._icon else 0)) / 2
        cy = h / 2 - 3
        if self._icon:
            self.create_image(x + 9, cy, image=self._icon)
            x += 26
        self.create_text(x, cy, text=self._text, anchor="w", fill="#ffffff", font=self._font)

    def _set(self, hover=None, down=None):
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down and self._state != "disabled"
        self._draw()

    def _release(self, event):
        was = self._down
        self._down = False
        self._draw()
        if was and self._state != "disabled" and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._state = "disabled" if kw.pop("state") == "disabled" else "normal"
            tk.Canvas.configure(self, cursor="" if self._state == "disabled" else "hand2")
        if "text" in kw:
            self._text = kw.pop("text")
        if "command" in kw:
            self._command = kw.pop("command")
        if cnf or kw:
            tk.Canvas.configure(self, cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return tk.Canvas.cget(self, key)

    def set_enabled(self, on):
        self.configure(state="normal" if on else "disabled")

    def set_text(self, text):
        self.configure(text=text)

    def set_icon(self, icon):
        self._icon = ui_icon(self, icon, 18, "white") if icon else None
        self._draw()

    def invoke(self):
        if self._state != "disabled":
            self._command()


BUTTON_ICONS = {"Browse…": "folder-open", "Browse...": "folder-open", "Choose…": "folder-open",
                "Choose...": "folder-open", "Add files…": "file-plus", "Add folder…": "folder", "Remove": "trash-2",
                "Clear": "brush", "Preview selected": "play", "Open subtitle file…": "file-text"}


def decorate(root):
    """Icons on the usual buttons (Browse, Add files, Remove...), and Cancel as plain text."""
    todo = [root]
    while todo:
        w = todo.pop()
        todo += w.winfo_children()
        if isinstance(w, ttk.Button) and not isinstance(w, PillButton):
            text = str(w.cget("text"))
            if text in BUTTON_ICONS and not str(w.cget("image")):
                w.config(image=ui_icon(w, BUTTON_ICONS[text], 15), compound="left")
            if text == "Cancel":
                w.config(style="Link.TButton" if _bg_of(w.master) == UI["bg"] else "LinkCard.TButton")


def _bg_of(w):
    try:
        return str(w.cget("background"))
    except tk.TclError:
        try:
            return ttk.Style(w).lookup(str(w.cget("style")) or w.winfo_class(), "background")
        except tk.TclError:
            return ""


def app_header(parent, title, tagline, logo=None, on_support=None):
    """The app's bar: a deep purple gradient with soft light swooshes, its icon, name and tagline
    (and, if given, a support link on the right)."""
    t, fam = UI, ui_family()
    c = tk.Canvas(parent, height=76, highlightthickness=0, bd=0, bg=t["purple_dark"])
    c._icon = tk.PhotoImage(master=c, data=HEADER_ICON)
    c._deco = ui_image(c, "header_deco")

    def draw(event=None):
        c.delete("all")
        w = max(c.winfo_width(), 300)
        a, b = (0x29, 0x15, 0x5e), (0x5b, 0x3c, 0xd8)
        steps = 64
        for i in range(steps):
            k = (i / (steps - 1)) ** 1.6
            col = "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3))
            c.create_rectangle(i * w / steps, 0, (i + 1) * w / steps + 1, 76, fill=col, outline=col)
        c.create_image(w, 0, image=c._deco, anchor="ne")
        c.create_image(46, 38, image=c._icon)
        c.create_text(90, 27, text=title, anchor="w", fill="#ffffff", font=(fam, 18, "bold"))
        c.create_text(91, 53, text=tagline, anchor="w", fill="#ddd5ff", font=(fam, 10))
        if on_support:
            heart = ui_icon(c, "heart", 15, "white")
            x = w - 22
            c.create_text(x, 38, text="Support this project", anchor="e", fill="#ffffff", font=(fam, 10, "underline"),
                          tags="support")
            c.create_image(x - tkfont_measure(fam, "Support this project") - 6, 38, image=heart, anchor="e",
                           tags="support")
            c.tag_bind("support", "<Button-1>", lambda e: on_support())
            c.tag_bind("support", "<Enter>", lambda e: c.config(cursor="hand2"))
            c.tag_bind("support", "<Leave>", lambda e: c.config(cursor=""))
    c.bind("<Configure>", draw)
    return c


def tkfont_measure(family, text, size=10, weight="normal"):
    import tkinter.font as tkfont
    return tkfont.Font(family=family, size=size, weight=weight).measure(text)


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link with a heart, opening the Ko-fi page in the browser."""
    import webbrowser
    bg = bg or UI["bg"]
    f = tk.Frame(parent, bg=bg, cursor="hand2")
    tk.Label(f, image=ui_icon(parent, "heart", 15, "pink"), bg=bg, bd=0).pack(side="left", padx=(0, 5))
    link = tk.Label(f, text="Support this project", fg=UI["error"], bg=bg, font=font or (ui_family(), 10, "underline"),
                    cursor="hand2")
    link.pack(side="left")
    for w in (f, link) + tuple(f.winfo_children()):
        w.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return f


THEME = UI  # (the names the app used before)
MUTED, ERROR = UI["muted"], UI["error"]


# The app's icon (64 px PNG), for the window and the taskbar. The .exe and the Mac app get
# theirs from assets/ when they're built.
APP_ICON = (
    "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"
    "AAAAAElFTkSuQmCC"
)

# The same icon, small, for the app's header bar.
HEADER_ICON = (
    "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"
    "dZf0AAAAAElFTkSuQmCC"
)


def set_app_icon(root):
    try:
        if sys.platform == "win32":
            # Its own taskbar button and icon, not Python's, when run as a script.
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OfflineToolkit.VideoCompressor")
        root._app_icon = tk.PhotoImage(master=root, data=APP_ICON)
        root.iconphoto(True, root._app_icon)
    except Exception:
        pass  # just the default icon


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    apply_theme(root)
    set_app_icon(root)
    CompressorApp(root)
    decorate(root)
    root.mainloop()


if __name__ == "__main__":
    main()
