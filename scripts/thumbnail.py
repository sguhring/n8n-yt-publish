"""Render a 1280x720 YouTube thumbnail from a video frame and a short text.

Prints a single JSON object on stdout for n8n: {"ok": true, "thumbnail_path": ...}.

Usage:
    python scripts/thumbnail.py --video inbox/example/clip.mp4 --text "Soft-boiled eggs"
                                --channel-config channels/example.json --out work/clip.jpg
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
MARGIN = 56
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguibl.ttf",   # Segoe UI Black
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def grab_frame(video: Path, at_fraction: float = 0.3) -> Image.Image:
    """Pick a representative frame near 30 % of the video with ffmpeg's thumbnail filter."""
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True).stdout.strip())
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{duration * at_fraction:.2f}",
             "-i", str(video), "-vf", "thumbnail=60", "-frames:v", "1", str(frame)],
            check=True)
        return Image.open(frame).convert("RGB")


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and centre-crop so the image fills w x h."""
    scale = max(w / img.width, h / img.height)
    resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_text(draw, text: str, max_w: int, max_h: int) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Largest font size at which the wrapped text fits the box (max 4 lines)."""
    for size in range(120, 30, -4):
        fnt = font(size)
        lines = wrap(draw, text.upper(), fnt, max_w)
        line_h = int(size * 1.08)
        if len(lines) <= 4 and line_h * len(lines) <= max_h and all(
                draw.textlength(l, font=fnt) <= max_w for l in lines):
            return fnt, lines, line_h
    fnt = font(32)
    return fnt, wrap(draw, text.upper(), fnt, max_w), 35


def render(frame: Image.Image, text: str, style: dict) -> Image.Image:
    bg = style.get("background", "#111111")
    accent = style.get("accent", "#FFCE00")
    text_color = style.get("text_color", "#FFFFFF")
    badge = style.get("badge", "")

    # background: the frame itself, blurred and darkened
    canvas = cover(frame, W, H).filter(ImageFilter.GaussianBlur(18))
    canvas = Image.blend(canvas, Image.new("RGB", (W, H), bg), 0.55)

    vertical = frame.height >= frame.width
    if vertical:
        # sharp frame on the right, text on the left
        fh = H - 2 * 36
        fw = round(frame.width * fh / frame.height)
        sharp = frame.resize((fw, fh), Image.LANCZOS)
        x = W - fw - MARGIN
        ImageDraw.Draw(canvas).rectangle((x - 6, 30, x + fw + 6, 30 + fh + 12), fill=accent)
        canvas.paste(sharp, (x, 36))
        text_box_w = x - 2 * MARGIN
    else:
        canvas = Image.blend(cover(frame, W, H), Image.new("RGB", (W, H), bg), 0.35)
        text_box_w = int(W * 0.62)

    draw = ImageDraw.Draw(canvas)
    y = MARGIN
    if badge:
        bfont = font(34)
        bw = int(draw.textlength(badge, font=bfont)) + 36
        draw.rounded_rectangle((MARGIN, y, MARGIN + bw, y + 58), radius=12, fill=accent)
        draw.text((MARGIN + 18, y + 8), badge, font=bfont, fill=bg)
        y += 90

    fnt, lines, line_h = fit_text(draw, text, text_box_w, H - y - MARGIN - 20)
    block_h = line_h * len(lines)
    y = max(y, (H - block_h) // 2 + (40 if badge else 0))
    for line in lines:
        draw.text((MARGIN + 4, y + 4), line, font=fnt, fill="#000000")  # drop shadow
        draw.text((MARGIN, y), line, font=fnt, fill=text_color)
        y += line_h
    draw.rectangle((MARGIN, y + 22, MARGIN + 160, y + 34), fill=accent)
    return canvas


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--text", required=True, help="2-6 words for the thumbnail")
    ap.add_argument("--channel-config", required=True)
    ap.add_argument("--out", required=True, help="output .jpg path")
    args = ap.parse_args()

    style = json.loads(Path(args.channel_config).read_text(encoding="utf-8")).get("thumbnail", {})
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    img = render(grab_frame(Path(args.video)), args.text, style)
    quality = 90
    img.save(out, "JPEG", quality=quality, optimize=True)
    while out.stat().st_size > 2_000_000 and quality > 50:  # YouTube limit: 2 MB
        quality -= 10
        img.save(out, "JPEG", quality=quality, optimize=True)

    print(json.dumps({"ok": True, "thumbnail_path": str(out), "bytes": out.stat().st_size}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
