"""Records the Flag Doctor demo video from a REAL run.

Every command below really runs (against Flagsmith Cloud and the Gemini API). Its output is
captured byte-by-byte with real timestamps and replayed into an MP4 through a terminal emulator.
Only two things are added for watchability:
  * commands are "typed" character by character, and prompts are answered after a short pause;
  * waits longer than MAX_IDLE seconds (AI thinking) are shortened to MAX_IDLE.
Nothing in the output is edited.

Usage (from the flag-doctor folder, after `flagsmith login`, with GEMINI_API_KEY in .env):
    pip install pyte pillow imageio-ffmpeg
    python docs/record_demo.py            # writes docs/demo.mp4 and docs/demo-recording.json
    python docs/record_demo.py --render   # re-render the video from the saved recording only
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_VIDEO = ROOT / "docs" / "demo.mp4"
OUT_EVENTS = ROOT / "docs" / "demo-recording.json"
FLAGDOCTOR = str(ROOT / ".venv" / "Scripts" / "flagdoctor.exe") if os.name == "nt" else "flagdoctor"
PROMPT = "PS D:\\Toggle-Wars\\flag-doctor> "

COLS, ROWS = 118, 40
FPS = 12
MAX_IDLE = 4.0       # longest silent gap kept in the video (seconds)
THINK = 2.5          # pause before a human answers a y/n prompt
END_PAUSE = 4.0

# (what is shown, what really runs or None for a comment)
STEPS = [
    ("# Flag Doctor - Toggle Wars Track 02 demo (Flagsmith Cloud + Flagsmith CLI)", None),
    ("# 1) Check-up: ask Flagsmith about every flag in the PawNest project", None),
    ("flagdoctor scan --repo ..\\pawnest-ab-test --use-cli",
     [FLAGDOCTOR, "scan", "--repo", "../pawnest-ab-test", "--use-cli"]),
    ("# 2) The A/B test finished and 'plan_visit' won. Let the AI remove the losing variant - I approve every file.", None),
    ("flagdoctor fix --repo ..\\pawnest-ab-test --use-cli --ai gemini",
     [FLAGDOCTOR, "fix", "--repo", "../pawnest-ab-test", "--use-cli", "--ai", "gemini"]),
    ("# 3) The cleanup is a normal git commit on its own branch, ready for review",
     None),
    ("git -C ..\\pawnest-ab-test log --oneline -3",
     ["git", "-C", "../pawnest-ab-test", "log", "--oneline", "-3"]),
]

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def human_answer(prompt: str, recent: str) -> str:
    """How the presenter answers each question."""
    if "Apply this change" in prompt:
        if "BROKEN CODE" in recent or "simulate-traffic" in prompt:
            return "n"  # never apply broken code; the simulator is a test tool that should keep reading the flag
        return "y"
    if "Create a new git branch" in prompt:
        return "y"
    return "n"  # no push (no GitHub CLI here), no archiving during the demo


# ---------------------------------------------------------------- recording
def record() -> list[list]:
    events: list[list] = []  # [seconds, text]
    clock = 0.0

    def emit(text: str, gap: float = 0.0):
        nonlocal clock
        clock += gap
        events.append([round(clock, 3), text])

    env = dict(os.environ, FORCE_COLOR="1", COLUMNS=str(COLS), LINES=str(ROWS), PYTHONIOENCODING="utf-8",
               PYTHONUNBUFFERED="1", TTY_COMPATIBLE="1", TTY_INTERACTIVE="0")
    emit(PROMPT, 1.0)
    for shown, argv in STEPS:
        for ch in shown:  # typing
            emit(ch, random.uniform(0.03, 0.08))
        emit("\r\n", 0.5)
        if argv is None:
            emit(PROMPT, 0.6)
            continue
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        start = time.monotonic()
        last_real = 0.0
        tail = ""
        chunks: list[tuple[float, str]] = []
        lock = threading.Lock()

        def reader():
            dec = __import__("codecs").getincrementaldecoder("utf-8")("replace")
            while True:
                b = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
                if not b:
                    break
                with lock:
                    chunks.append((time.monotonic() - start, dec.decode(b)))

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        while True:
            time.sleep(0.05)
            with lock:
                pending, chunks[:] = list(chunks), []
            for real_t, text in pending:
                gap = min(real_t - last_real, MAX_IDLE)
                last_real = real_t
                emit(text.replace("\r\n", "\n").replace("\n", "\r\n"), max(gap, 0))
                tail = (tail + text)[-4000:]
            plain = ANSI.sub("", tail)
            m = re.search(r"([^\n]*\[y/n\][^\n]*:\s*)$", plain)
            if m and proc.poll() is None:
                answer = human_answer(m.group(1), plain)
                emit(answer, THINK)
                emit("\r\n", 0.3)
                proc.stdin.write((answer + "\n").encode())
                proc.stdin.flush()
                tail = ""
                last_real = time.monotonic() - start
            if proc.poll() is not None and not t.is_alive():
                break
        emit(PROMPT, 0.8)
        print(f"  recorded: {shown}  (exit {proc.returncode})", file=sys.stderr)
    emit("", END_PAUSE)
    return events


# ---------------------------------------------------------------- rendering
PALETTE = {
    "default": (204, 204, 204), "black": (12, 12, 12), "red": (231, 72, 86), "green": (22, 198, 12),
    "brown": (193, 156, 0), "yellow": (193, 156, 0), "blue": (59, 120, 255), "magenta": (180, 0, 158),
    "cyan": (58, 150, 221), "white": (204, 204, 204),
    "brightblack": (118, 118, 118), "brightred": (231, 72, 86), "brightgreen": (22, 198, 12),
    "brightbrown": (249, 241, 165), "brightyellow": (249, 241, 165), "brightblue": (59, 120, 255),
    "brightmagenta": (180, 0, 158), "brightcyan": (97, 214, 214), "brightwhite": (242, 242, 242),
}
BG = (12, 12, 12)


def _color(name: str, default):
    if name == "default":
        return default
    if re.fullmatch(r"[0-9a-fA-F]{6}", name):
        return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))
    return PALETTE.get(name, default)


ROUNDED = {"╭": "┌", "╮": "┐", "╰": "└", "╯": "┘"}


def is_emoji(c: str) -> bool:
    o = ord(c)
    # Box drawing / blocks (U+2500-U+259F) and arrows stay in Consolas; real emoji use the emoji font.
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF


def render(events: list[list]) -> None:
    import imageio_ffmpeg
    import pyte
    from PIL import Image, ImageDraw, ImageFont

    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    reg = ImageFont.truetype(str(fonts / "consola.ttf"), 16)
    bold = ImageFont.truetype(str(fonts / "consolab.ttf"), 16)
    emoji = ImageFont.truetype(str(fonts / "seguiemj.ttf"), 14)
    cw, ch = int(round(reg.getlength("M"))), 20
    pad, bar = 14, 34
    width = COLS * cw + 2 * pad
    height = ROWS * ch + 2 * pad + bar
    width, height = (width + 15) // 16 * 16, (height + 15) // 16 * 16  # codec-friendly size

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)

    def draw_frame() -> bytes:
        img = Image.new("RGB", (width, height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, width, bar], fill=(32, 32, 32))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([12 + i * 22, 11, 24 + i * 22, 23], fill=c)
        d.text((90, 8), "Windows PowerShell  -  flag-doctor", font=reg, fill=(200, 200, 200))
        for y in range(ROWS):
            line = screen.buffer[y]
            for x in range(COLS):
                cell = line[x]
                if not cell.data:
                    continue
                fg = _color(cell.fg, PALETTE["default"])
                bgc = _color(cell.bg, BG)
                if cell.reverse:
                    fg, bgc = bgc, fg
                px, py = pad + x * cw, bar + pad + y * ch
                if bgc != BG:
                    d.rectangle([px, py, px + cw * max(1, unicodedata.east_asian_width(cell.data[0]) in "WF" and 2 or 1), py + ch], fill=bgc)
                c0 = cell.data[0]
                if c0 in ROUNDED:  # Consolas has no rounded corners
                    c0 = ROUNDED[c0]
                    cell = cell._replace(data=c0)
                if c0 == " ":
                    continue
                if is_emoji(c0):
                    d.text((px, py + 2), cell.data, font=emoji, embedded_color=True)
                else:
                    d.text((px, py + 1), cell.data, font=bold if cell.bold else reg, fill=fg)
        if screen.cursor.y < ROWS:
            cx, cy = pad + screen.cursor.x * cw, bar + pad + screen.cursor.y * ch
            d.rectangle([cx, cy + ch - 4, cx + cw - 1, cy + ch - 2], fill=(204, 204, 204))
        return img.tobytes()

    total = events[-1][0] if events else 0
    writer = imageio_ffmpeg.write_frames(str(OUT_VIDEO), (width, height), fps=FPS, codec="libx264",
                                         pix_fmt_out="yuv420p", output_params=["-crf", "23", "-preset", "medium"])
    writer.send(None)
    i, frame, last = 0, None, None
    n_frames = int(total * FPS) + 1
    for f in range(n_frames):
        t = f / FPS
        changed = False
        while i < len(events) and events[i][0] <= t:
            stream.feed(events[i][1])
            i += 1
            changed = True
        if changed or frame is None:
            frame = draw_frame()
        writer.send(frame)
    writer.close()
    print(f"video: {OUT_VIDEO}  ({total:.0f}s, {width}x{height})", file=sys.stderr)


if __name__ == "__main__":
    if "--render" in sys.argv:
        events = json.loads(OUT_EVENTS.read_text(encoding="utf-8"))
    else:
        events = record()
        OUT_EVENTS.write_text(json.dumps(events), encoding="utf-8")
    render(events)
