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
        main = ttk.Frame(self.root, padding=(12, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        f = ttk.LabelFrame(main, text="1. Video", padding=8)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.v["src"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="Browse…", command=self.pick_video).grid(row=0, column=1, padx=(6, 0))
        self.info_lbl = ttk.Label(f, text="No video selected", foreground=MUTED, wraplength=500)
        self.info_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Target size", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Combobox(f, values=list(PRESETS), textvariable=self.v["preset"],
                     state="readonly", width=30).grid(row=0, column=0, sticky="w", **pad)
        self.target_e = ttk.Entry(f, textvariable=self.v["target"], width=8)
        self.target_e.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="MB").grid(row=0, column=2, sticky="w")
        ttk.Label(f, text="Upload limits change over time. Pick Custom to type any size.",
                  foreground=MUTED).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="3. Part of the video (optional)", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Start").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v["start"], width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="End").grid(row=0, column=2, sticky="w")
        ttk.Entry(f, textvariable=self.v["end"], width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(f, text="Blank = whole video. Seconds (12.5) or m:ss (1:05). "
                          "A shorter part gets better quality at the same size.",
                  foreground=MUTED, wraplength=500).grid(row=1, column=0, columnspan=4, sticky="w")

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
        self.plan_lbl = ttk.Label(f, text="", foreground=MUTED, wraplength=500, justify="left")
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
        self.go_btn = PillButton(f, "Compress", self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        foot = ttk.Frame(main)  # the status line, with the support link on its right
        foot.grid(row=6, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=370)
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


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link that opens the Ko-fi page in the browser."""
    import webbrowser
    kw = {"bg": bg} if bg else {}
    link = tk.Label(parent, text="\u2665 Support this project", fg="#c8364f", cursor="hand2",
                    font=font or ("", 9, "underline"), **kw)
    link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return link


# --------------------------------------------------------------------------- #
#  The look (shared by the Offline Toolkit apps): white cards, a dark purple
#  header fading to blue, blue actions. Only colours and fonts: no behaviour.
# --------------------------------------------------------------------------- #
THEME = dict(
    bg="#ffffff", card="#ffffff", border="#e2ddf0", text="#1c1433", muted="#6e6787", error="#c8364f",
    purple="#2b1655", purple2="#4a2a8a", blue="#2f7cf6", blue_hover="#1d67dc", blue_soft="#e7f0ff",
    blue_text="#1756c4", lavender_line="#7b5cd6", disabled="#c9c4d8", tint="#f5f1ff")
MUTED, ERROR = THEME["muted"], THEME["error"]


def ui_family():
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for name in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def _indicator_image(root, kind, on, enabled, n=18, k=4):
    """A checkbox (a blue box with a white tick) or radio button (a blue ring and dot),
    drawn pixel by pixel with smooth edges (no image library needed)."""
    t = THEME
    blue = t["blue"] if enabled else t["disabled"]
    edge = "#b9b0d6" if enabled else t["disabled"]

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - u * dx) ** 2 + (py - ay - u * dy) ** 2) ** 0.5

    def in_round_rect(x, y, r=4.6, lo=0.9, hi=n - 0.9):
        cx, cy = min(max(x, lo + r), hi - r), min(max(y, lo + r), hi - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def colour_at(x, y):
        """The colour of one sample point, or None where it's see-through."""
        c = n / 2
        d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
        if kind == "check":
            if not in_round_rect(x, y):
                return None
            if on:
                tick = min(seg_dist(x, y, 4.8, 9.4, 7.8, 12.4), seg_dist(x, y, 7.8, 12.4, 13.4, 5.9))
                return "#ffffff" if tick <= 1.15 else blue
            return edge if not in_round_rect(x, y, 3.4, 2.3, n - 2.3) else "#ffffff"
        if d > n / 2 - 0.9:
            return None
        if on:
            return blue if d > n / 2 - 2.5 or d < 3.6 else "#ffffff"
        return edge if d > n / 2 - 2.3 else "#ffffff"

    img = tk.PhotoImage(master=root, width=n, height=n)
    for py in range(n):
        row = []
        for px in range(n):
            acc, hits = [0, 0, 0], 0
            for sy in range(k):
                for sx in range(k):
                    col = colour_at(px + (sx + 0.5) / k, py + (sy + 0.5) / k)
                    if col is not None:
                        r, g, b = rgb(col)
                        acc[0] += r
                        acc[1] += g
                        acc[2] += b
                        hits += 1
            if hits == 0:
                row.append(None)
                continue
            # Blend with white for partly covered edge pixels (the widgets sit on white).
            a = hits / (k * k)
            r, g, b = (round(acc[i] / hits * a + 255 * (1 - a)) for i in range(3))
            row.append("#%02x%02x%02x" % (r, g, b))
        for px, col in enumerate(row):
            if col is not None:
                img.put(col, (px, py))
    return img


def apply_theme(root):
    """ttk's "clam" theme, recoloured (the native Windows and macOS themes ignore colours)."""
    import tkinter.font as tkfont
    t, fam = THEME, ui_family()
    root.configure(bg=t["bg"])
    root.option_add("*Background", t["bg"])  # plain Tk widgets (pictures, links) sit on white too
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=fam, size=10)
        except tk.TclError:
            pass
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=t["bg"], foreground=t["text"], font=(fam, 10), bordercolor=t["border"],
                focuscolor=t["blue"], lightcolor=t["card"], darkcolor=t["border"], troughcolor="#ece8f7")
    s.map(".", foreground=[("disabled", t["disabled"])])
    s.map("TLabel", background=[("disabled", t["card"])])  # clam greys a disabled label's background
    s.configure("TLabelframe", background=t["card"], bordercolor=t["border"], lightcolor=t["border"],
                darkcolor=t["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=t["card"], foreground=t["purple2"], font=(fam, 11, "bold"))
    for kind in ("TCheckbutton", "TRadiobutton"):
        s.configure(kind, background=t["card"], foreground=t["text"])
        s.map(kind, background=[("active", t["card"])], foreground=[("disabled", t["disabled"])])
    s.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=t["text"],
                arrowcolor=t["purple2"], bordercolor=t["border"], padding=(6, 3), selectbackground="#ffffff",
                selectforeground=t["text"])
    s.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f6f4fb")],
          foreground=[("disabled", t["disabled"])], arrowcolor=[("disabled", t["disabled"])],
          bordercolor=[("focus", t["blue"]), ("hover", t["lavender_line"])])
    s.configure("TEntry", fieldbackground="#ffffff", bordercolor=t["border"], padding=(6, 3))
    s.map("TEntry", bordercolor=[("focus", t["blue"])], fieldbackground=[("disabled", "#f6f4fb")])
    s.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=t["border"], arrowcolor=t["purple2"], padding=(6, 3))
    s.configure("TButton", background="#ffffff", foreground=t["purple2"], bordercolor=t["border"],
                padding=(12, 5), font=(fam, 10, "bold"), relief="flat")
    s.map("TButton", background=[("disabled", t["card"]), ("pressed", t["tint"]), ("active", "#f7f4ff")],
          foreground=[("disabled", t["disabled"])], bordercolor=[("active", t["lavender_line"])])
    s.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=t["text"],
                rowheight=26, font=(fam, 10))
    s.map("Treeview", background=[("selected", t["blue_soft"])], foreground=[("selected", t["blue_text"])])
    s.configure("Treeview.Heading", background="#f6f4fb", foreground=t["muted"], font=(fam, 9, "bold"),
                relief="flat", padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", "#efeaf9")])
    s.configure("Horizontal.TProgressbar", troughcolor="#ece8f7", background=t["blue"], bordercolor="#ece8f7",
                lightcolor=t["blue"], darkcolor=t["blue"])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background="#ddd6ee", troughcolor="#f6f4fb", bordercolor="#f6f4fb",
                    arrowcolor=t["muted"], lightcolor="#ddd6ee", darkcolor="#ddd6ee", gripcount=0)
    s.configure("Horizontal.TScale", background=t["blue"], troughcolor="#ece8f7", bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    # Checkboxes and radio buttons: drawn ticks and dots (clam draws a tick as an X).
    imgs = root._indicator_images = {(kind, on, en): _indicator_image(root, kind, on, en)
                                     for kind in ("check", "radio") for on in (False, True) for en in (False, True)}
    for kind, widget in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = f"Toolkit.{kind}.indicator"
        try:
            s.element_create(name, "image", imgs[(kind, False, True)],
                             ("disabled", "selected", imgs[(kind, True, False)]),
                             ("disabled", imgs[(kind, False, False)]),
                             ("selected", imgs[(kind, True, True)]), padding=(0, 0, 6, 0), sticky="")
        except tk.TclError:
            pass
        s.layout(f"T{widget}", [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})])
    return fam


class PillButton(tk.Canvas):
    """A rounded blue button for the app's main action. Accepts .config(state=..., text=...)
    like a ttk.Button, so the rest of the app drives it the same way."""

    def __init__(self, parent, text, command, height=36, padx=24):
        import tkinter.font as tkfont
        self._font = tkfont.Font(family=ui_family(), size=11, weight="bold")
        self._padx = padx
        self._bh = height
        self._bw = self._font.measure(text) + 2 * padx
        super().__init__(parent, width=self._bw, height=height, bg=THEME["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._text, self._command, self._enabled, self._hover = text, command, True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        t = THEME
        fill = t["disabled"] if not self._enabled else t["blue_hover"] if self._hover else t["blue"]
        r, w, h = self._bh // 2, self._bw, self._bh
        pts = [1 + r, 1, w - 1 - r, 1, w - 1, 1, w - 1, 1 + r, w - 1, h - 1 - r, w - 1, h - 1, w - 1 - r, h - 1,
               1 + r, h - 1, 1, h - 1, 1, h - 1 - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill="#ffffff", font=self._font)

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _click(self, event):
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "")
        if "text" in kw:
            self._text = kw.pop("text")
            self._bw = self._font.measure(self._text) + 2 * self._padx
            super().configure(width=self._bw)
        if "command" in kw:
            self._command = kw.pop("command")
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self):
        if self._enabled:
            self._command()


def app_header(parent, title, tagline, logo):
    """The app's bar: dark purple fading to blue, with its logo, name and what it does."""
    t, fam = THEME, ui_family()
    c = tk.Canvas(parent, height=60, highlightthickness=0, bd=0, bg=t["purple"])

    def draw(event=None):
        c.delete("all")
        w = max(c.winfo_width(), 300)
        a, b = (0x2b, 0x16, 0x55), (0x2f, 0x5c, 0xd6)
        steps = 48
        for i in range(steps):
            k = (i / (steps - 1)) ** 2.2  # mostly purple, turning blue on the right
            col = "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3))
            c.create_rectangle(i * w / steps, 0, (i + 1) * w / steps + 1, 60, fill=col, outline=col)
        if not hasattr(c, "_logo"):  # the app's own icon
            c._logo = tk.PhotoImage(master=c, data=HEADER_ICON)
        c.create_image(35, 30, image=c._logo)
        c.create_text(68, 21, text=title, anchor="w", fill="#ffffff", font=(fam, 15, "bold"))
        c.create_text(69, 42, text=tagline, anchor="w", fill="#cfc6ff", font=(fam, 9))
    c.bind("<Configure>", draw)
    return c


# The app's icon (64 px PNG), for the window and the taskbar. The .exe and the Mac app get
# theirs from assets/ when they're built.
APP_ICON = (
    "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"
    "AAAAAElFTkSuQmCC"
)

# The same icon, small, for the app's header bar.
HEADER_ICON = (
    "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"
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
    root.mainloop()


if __name__ == "__main__":
    main()
