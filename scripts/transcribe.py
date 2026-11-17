"""Probe and transcribe one video for the n8n workflow.

Prints a single JSON object on stdout (n8n's Execute Command node reads it);
progress messages go to stderr.

Usage:
    python scripts/transcribe.py --video inbox/example/clip.mp4 [--channel example]
                                 [--model small] [--language de]

Optional files next to the video, matched by base name (clip.mp4 -> clip.*):
    clip.jpg / clip.png   thumbnail to use instead of a rendered one
    clip.txt / clip.md    extra instructions for the LLM that writes the metadata
Both are looked up after transcription, so they may be dropped a few seconds
after the video.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
THUMB_EXT = (".jpg", ".jpeg", ".png")
INSTRUCTION_EXT = (".txt", ".md")
INSTRUCTION_MAX_CHARS = 2000  # keeps a stray long file from crowding out the transcript
SHORTS_MAX_SECONDS = 180  # YouTube treats vertical/square videos up to 3 minutes as Shorts


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def probe(video: Path) -> dict:
    """Duration and frame size of the first video stream via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    stream = info["streams"][0]
    duration = float(info["format"]["duration"])
    width, height = int(stream["width"]), int(stream["height"])
    return {
        "duration_s": round(duration, 2),
        "width": width,
        "height": height,
        "is_short": duration <= SHORTS_MAX_SECONDS and height >= width,
    }


def find_thumbnail(video: Path) -> str | None:
    """A thumbnail dropped next to the video with the same base name, if any."""
    for ext in THUMB_EXT:
        candidate = video.with_suffix(ext)
        if candidate.exists():
            return str(candidate.resolve())
    return None


def find_instructions(video: Path) -> tuple[str | None, str | None]:
    """Optional per-video instructions for the metadata LLM: clip.txt or clip.md."""
    for ext in INSTRUCTION_EXT:
        candidate = video.with_suffix(ext)
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8-sig", errors="replace").strip()
            if not text:
                return None, str(candidate.resolve())
            if len(text) > INSTRUCTION_MAX_CHARS:
                log(f"instructions in {candidate.name} cut to {INSTRUCTION_MAX_CHARS} characters")
                text = text[:INSTRUCTION_MAX_CHARS]
            log(f"using instructions from {candidate.name} ({len(text)} characters)")
            return text, str(candidate.resolve())
    return None, None


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcribe(video: Path, model_name: str, language: str | None) -> dict:
    from faster_whisper import WhisperModel  # imported late: slow import

    log(f"loading whisper model '{model_name}' (cpu, int8)")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(video), language=language, vad_filter=True)
    segs = [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments
    ]
    log(f"transcribed {len(segs)} segments, language={info.language}")
    return {
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "text": " ".join(s["text"] for s in segs),
        # compact timestamped form for the LLM (used for chapters on long videos)
        "timestamped": "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segs),
        "segments": segs,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--channel", help="channel id; default = name of the video's folder")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "small"))
    ap.add_argument("--language", default=None, help="force the audio language, e.g. de")
    args = ap.parse_args()

    video = Path(args.video).resolve()
    if not video.exists():
        print(json.dumps({"ok": False, "error": f"video not found: {video}"}))
        return 1
    if video.suffix.lower() not in VIDEO_EXT:
        print(json.dumps({"ok": False, "error": f"not a video file: {video.name}"}))
        return 1

    channel = args.channel or video.parent.name
    media = probe(video)
    transcript = transcribe(video, args.model, args.language)
    # side files are looked up after the slow transcription step on purpose
    instructions, instructions_path = find_instructions(video)
    result = {
        "ok": True,
        "video_path": str(video),
        "file_name": video.name,
        "base_name": video.stem,
        "channel": channel,
        "thumbnail_path": find_thumbnail(video),
        "instructions": instructions,
        "instructions_path": instructions_path,
        **media,
        "transcript": transcript,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
