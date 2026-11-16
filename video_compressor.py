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
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        f = ttk.LabelFrame(main, text="1. Video", padding=8)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.v["src"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="Browse…", command=self.pick_video).grid(row=0, column=1, padx=(6, 0))
        self.info_lbl = ttk.Label(f, text="No video selected", foreground="gray", wraplength=500)
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Target size", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Combobox(f, values=list(PRESETS), textvariable=self.v["preset"],
                     state="readonly", width=30).grid(row=0, column=0, sticky="w", **pad)
        self.target_e = ttk.Entry(f, textvariable=self.v["target"], width=8)
        self.target_e.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="MB").grid(row=0, column=2, sticky="w")
        ttk.Label(f, text="Upload limits change over time. Pick Custom to type any size.",
                  foreground="gray").grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="3. Part of the video (optional)", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Start").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["start"], width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="End").grid(row=0, column=2, sticky="w")
        ttk.Entry(f, textvariable=self.v["end"], width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(f, text="Blank = whole video. Seconds (12.5) or m:ss (1:05). "
                          "A shorter part gets better quality at the same size.",
                  foreground="gray", wraplength=500).grid(row=1, column=0, columnspan=4, sticky="w")

        f = ttk.LabelFrame(main, text="4. Quality", padding=8)
        f.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Resolution").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=RESOLUTIONS, textvariable=self.v["res"],
                     state="readonly", width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Frame rate").grid(row=0, column=2, sticky="w", **pad)
        ttk.Combobox(f, values=FRAME_RATES, textvariable=self.v["fps"],
                     state="readonly", width=10).grid(row=0, column=3, sticky="w", **pad)
        self.audio_w = ttk.Checkbutton(f, text="Keep sound", variable=self.v["audio"])
        self.audio_w.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.plan_lbl = ttk.Label(f, text="", foreground="gray", wraplength=500, justify="left")
        self.plan_lbl.grid(row=2, column=0, columnspan=4, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="5. Save to", padding=8)
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["out_dir"]).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Choose…", command=self.pick_folder).grid(row=0, column=2)
        ttk.Label(f, text="File name").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=self.v["out_name"]).grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Checkbutton(f, text="Show the file in its folder when done",
                        variable=self.v["reveal"]).grid(row=2, column=1, sticky="w", pady=(4, 0))

        f = ttk.Frame(main)
        f.grid(row=5, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.go_btn = ttk.Button(f, text="Compress", command=self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        self.status = ttk.Label(main, text="Ready.", wraplength=520)
        self.status.grid(row=6, column=0, sticky="w", **pad)

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
                                 foreground="gray")
            return
        try:
            _, duration, p = self._plan()
        except ValueError as e:
            self.plan_lbl.config(text=str(e).replace("\n\n", " "), foreground="red")
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
        self.plan_lbl.config(text=text, foreground="red" if poor else "gray")

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
                                 foreground="red")
            return
        try:
            self.info = probe_video(self.ffmpeg, path)
        except OSError as e:  # FFmpeg binary couldn't be launched at all
            self.info = dict(error=f"Couldn't run FFmpeg: {e}")
        self.info_path = path
        i = self.info
        if i["error"]:
            self.info_lbl.config(text=f"Couldn't read video: {i['error']}", foreground="red")
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
            return self._finish("Cancelled.")
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


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    try:  # nicer native look where available
        ttk.Style().theme_use("vista" if sys.platform == "win32" else
                              "aqua" if sys.platform == "darwin" else "clam")
    except tk.TclError:
        pass
    CompressorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
