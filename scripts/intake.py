"""Check a folder sent to the Telegram bot and stage its files for the n8n workflow.

The folder holds one video, optionally a text file with title / description / tags,
and up to two thumbnails (A and B, in file-name order). The script finds them, reads
the text file, links or copies the files into work/ (n8n may only read files inside
the repo) and prints a single JSON object on stdout; progress goes to stderr.

Usage:
    python scripts/intake.py --path-hex <hex of the UTF-8 path> [--repo <repo>]
    python scripts/intake.py --path "C:/Users/me/Videos/clip-17"

The path comes from a chat message, so the workflow passes the message hex-encoded:
nothing from it ever reaches the shell as text.

Visibility (private, unlisted or public) and publish date, first match wins:
    1. words after the path in the message, or on their own line:  "C:/Videos/clip-17" public
                                                                    "C:/Videos/clip-17" 18.11. 18:00
    2. lines in the text file:                                     Privacy: unlisted / Publish: morgen 18 Uhr
    3. DEFAULT_PRIVACY in .env, else private
German and Spanish words work too (öffentlich, privat, nicht gelistet; público, privado;
morgen, Freitag, 18 Uhr; mañana, viernes). A date and time means: private until then, then
public (YouTube's publishAt). The time is the PC's local time and must be 15+ minutes ahead.

Linked video: a YouTube link after the path (or "Related:" in the text file, or the app field) goes
at the top of the description. Without one, Shorts link the channel's latest public long video
(DEFAULT_RELATED=latest; "none" turns it off, "no link" after the path skips it once). The Shorts
"Related video" field itself can only be set in YouTube Studio; the API has no field for it.

Text file (meta.txt, or any single .txt/.md in the folder), every part optional:
    Title: Soft-boiled eggs in 6½ minutes
    Tags: eggs, breakfast, kitchen tips
    Channel: example
    Privacy: public
    Publish: 18.11.2026 18:00
    Related: https://youtu.be/VIDEO_ID   (or: none)
    Instructions: hints for the LLM if it has to write something
    Description:
    Everything from here on is the description,
    over as many lines as you like.
German and Spanish keys work too (Titel, Beschreibung, Kanal, Anweisungen; Título,
Descripción, Etiquetas, Canal). A file without any key is read as the description.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe import VIDEO_EXT, probe  # noqa: E402  (same folder)

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
TEXT_EXT = {".txt", ".md"}
META_NAMES = {"meta", "metadata", "info", "youtube", "yt", "beschreibung", "description", "descripcion"}
THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube rejects custom thumbnails above 2 MB
WORK_KEEP_SECONDS = 3 * 24 * 3600  # staged copies older than this are removed
INSTRUCTIONS_MAX_CHARS = 2000

KEYS = {
    "title": ("title", "titel", "título", "titulo"),
    "description": ("description", "beschreibung", "descripción", "descripcion", "desc"),
    "tags": ("tags", "tag", "schlagwörter", "schlagworte", "keywords", "etiquetas"),
    "channel": ("channel", "kanal", "canal"),
    "privacy": ("privacy", "visibility", "sichtbarkeit", "privatsphäre", "privacidad", "visibilidad"),
    "schedule": ("schedule", "publish at", "publish on", "publish", "planen", "geplant", "veröffentlichung",
                 "veröffentlichen am", "programar", "programado", "publicar"),
    "related": ("related", "related video", "linked video", "verknüpftes video", "verlinktes video",
                "video relacionado", "vídeo relacionado"),
    "instructions": ("instructions", "instruction", "anweisungen", "anweisung", "hinweise",
                     "hinweis", "prompt", "instrucciones"),
}
KEY_OF = {alias: key for key, aliases in KEYS.items() for alias in aliases}
_alias = "|".join(sorted((re.escape(a) for a in KEY_OF), key=len, reverse=True))
# "Title: ..." / "**Tags:** ..." or a Markdown heading "## Description"
HEADER = re.compile(
    rf"^\s*(?:#{{1,6}}\s*)?\**({_alias})\**\s*:\**\s*(.*)$|^\s*#{{1,6}}\s*({_alias})\s*$",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class IntakeError(Exception):
    pass


PRIVACY_WORDS = {
    "private": ("private", "privat", "privado", "privada"),
    "unlisted": ("unlisted", "ungelistet", "nicht gelistet", "nichtgelistet", "no listado", "nolistado"),
    "public": ("public", "öffentlich", "oeffentlich", "publico", "público", "publish", "veröffentlichen"),
}
PRIVACY_OF = {w: k for k, words in PRIVACY_WORDS.items() for w in words}
QUOTES = "\"'“”‘’"


def privacy_of(text: str) -> str | None:
    """'Public!' -> public, 'nicht gelistet' -> unlisted, anything else -> None."""
    key = " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())
    return PRIVACY_OF.get(key)


SCHEDULE_FILLER = {"plan", "planen", "geplant", "schedule", "scheduled", "programar", "programado", "programada",
                   "am", "um", "ab", "at", "on", "for", "el", "a", "la", "las", "para", "für", "fuer", "uhr", "h"}
RELATIVE_DAYS = {"heute": 0, "today": 0, "hoy": 0, "morgen": 1, "tomorrow": 1, "mañana": 1, "manana": 1,
                 "übermorgen": 2, "uebermorgen": 2}
WEEKDAYS = {name: i for i, names in enumerate((
    ("montag", "monday", "mon", "lunes"), ("dienstag", "tuesday", "tue", "martes"),
    ("mittwoch", "wednesday", "wed", "miércoles", "miercoles"), ("donnerstag", "thursday", "thu", "jueves"),
    ("freitag", "friday", "fri", "viernes"), ("samstag", "saturday", "sat", "sábado", "sabado"),
    ("sonntag", "sunday", "sun", "domingo"))) for name in names}
DATE_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?[./]?$")      # 18.11. 18.11.2026 18/11
DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:t(\d{1,2}):(\d{2}))?$")      # 2026-11-18 2026-11-18t18:00
TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?(h|uhr)?$")                            # 18:00 18:30h 18h 18uhr
MIN_LEAD = timedelta(minutes=15)  # upload + thumbnail take a few minutes; YouTube needs a future publishAt

# Linked video: a YouTube link after the path (or Related: in the text file) is put at the top of the
# description; Shorts get the channel's latest long video by default (DEFAULT_RELATED=latest|none).
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_URL = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtu\.be/|youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/))"
                   r"([A-Za-z0-9_-]{11})\S*", re.IGNORECASE)
NO_LINK = {"no link", "nolink", "ohne link", "kein link", "sin enlace", "sin link", "none", "keins"}
RELATED_FILLER = {"related", "linked", "verknüpft", "verknuepft", "verlinkt", "relacionado", "video", "vídeo"}


@dataclass
class Options:
    """What the words after the path (or the Privacy/Publish lines of the text file) ask for."""
    privacy: str | None = None
    schedule: bool = False          # a date or time was given
    when: datetime | None = None    # naive, the PC's local time
    problem: str | None = None
    related: str | None = None      # a YouTube video id, or "none"
    unknown: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        """Says something about visibility (a linked video alone does not)."""
        return bool(self.privacy or self.schedule)


def parse_options(text: str, now: datetime) -> Options:
    """'public', 'morgen 18 Uhr', '18.11. 18:00', 'Freitag 9:30 unlisted', 'youtu.be/…', ... -> Options."""
    o = Options()
    text = str(text or "")
    for m in YT_URL.finditer(text):  # before lower-casing: video ids are case-sensitive
        o.related = m[1]
    text = YT_URL.sub(" ", text)
    toks = [t.strip("!?()[]{}" + QUOTES) for t in re.split(r"[\s,;]+", text.lower())]
    toks = [t for t in toks if t]
    day: date | None = None
    year_given = True
    rel = wd = hm = None
    i = 0
    while i < len(toks):
        t, nxt = toks[i], toks[i + 1] if i + 1 < len(toks) else ""
        if nxt and privacy_of(f"{t} {nxt}"):  # "nicht gelistet", "no listado"
            o.privacy = privacy_of(f"{t} {nxt}")
            i += 2
            continue
        if t == "pasado" and nxt in ("mañana", "manana"):
            rel = 2
            i += 2
            continue
        if f"{t} {nxt}" in NO_LINK or t in NO_LINK:
            o.related = "none"
            i += 2 if f"{t} {nxt}" in NO_LINK else 1
            continue
        m_iso, m_dmy, m_time = DATE_ISO.match(t), DATE_DMY.match(t), TIME.match(t.rstrip("."))
        try:
            if privacy_of(t):
                o.privacy = privacy_of(t)
            elif t in RELATIVE_DAYS:
                rel = RELATIVE_DAYS[t]
            elif t in WEEKDAYS:
                wd = WEEKDAYS[t]
            elif m_iso:
                day = date(int(m_iso[1]), int(m_iso[2]), int(m_iso[3]))
                if m_iso[4]:
                    hm = dtime(int(m_iso[4]), int(m_iso[5]))
            elif m_dmy:
                year_given = bool(m_dmy[3])
                y = int(m_dmy[3]) if m_dmy[3] else now.year
                day = date(y + 2000 if y < 100 else y, int(m_dmy[2]), int(m_dmy[1]))
            elif m_time and (m_time[2] or m_time[3] or nxt in ("uhr", "h")):
                hm = dtime(int(m_time[1]), int(m_time[2] or 0))
            elif t not in SCHEDULE_FILLER and t not in RELATED_FILLER:
                o.unknown.append(t)
        except ValueError:  # 31.02., 25:00
            o.unknown.append(t)
        i += 1

    o.schedule = any(x is not None for x in (day, rel, wd, hm))
    if not o.schedule:
        return o
    if hm is None:
        o.problem = "A publish date needs a time, e.g. 18.11. 18:00"
        return o
    if day:
        when = datetime.combine(day, hm)
        if not year_given and when < now - timedelta(days=60):  # "05.01." written in December
            when = when.replace(year=when.year + 1)
    elif rel is not None:
        when = datetime.combine(now.date() + timedelta(days=rel), hm)
    elif wd is not None:
        when = datetime.combine(now.date() + timedelta(days=(wd - now.weekday()) % 7), hm)
        if when <= now:
            when += timedelta(days=7)
    else:  # a time alone: today, or tomorrow if that time has passed
        when = datetime.combine(now.date(), hm)
        if when <= now:
            when += timedelta(days=1)
    o.when = when
    return o


def show_time(local: datetime) -> str:
    """Wed 18 Nov 2026, 18:00 (UTC+00:00)"""
    aware = local.astimezone()
    off = aware.strftime("%z")
    return f"{aware:%a %d %b %Y, %H:%M} (UTC{off[:3]}:{off[3:]})"


def understood(text: str) -> bool:
    o = parse_options(text, datetime.now())
    return bool(o.found or o.related) and not o.unknown


def split_message(raw: str) -> tuple[str, str]:
    """Path and the words after it (visibility, publish date) from a chat message.

    The words may follow the path on the first line ("C:/clip" public, or C:/clip morgen 18 Uhr)
    or stand on lines of their own."""
    lines = [ln.strip() for ln in str(raw).strip().splitlines() if ln.strip()]
    if not lines:
        return "", ""
    first, extra = lines[0], " ".join(lines[1:])
    join = lambda *parts: " ".join(p for p in parts if p).strip()  # noqa: E731
    if first[:1] in QUOTES:
        end = max(first.rfind(q) for q in QUOTES)
        if end > 0:
            return first[1:end], join(first[end + 1:], extra)
    exists = lambda s: Path(os.path.expandvars(s.strip(QUOTES))).exists()  # noqa: E731
    words = first.split()
    cands = [(" ".join(words[:-n]), " ".join(words[-n:])) for n in range(len(words) - 1, 0, -1)
             if understood(" ".join(words[-n:]))]  # longest understood tail first
    for rest, tail in cands:
        if exists(rest):
            return rest, join(tail, extra)
    # a folder really named "... public" wins over the keyword
    if exists(first) or not cands:
        return first, extra
    return cands[0][0], join(cands[0][1], extra)


def clean_path(raw: str) -> Path:
    """A path as pasted from Explorer: quotes, file:// prefix, %VARS% and ~ allowed."""
    text = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    text = text.strip("\"'“”‘’ ").strip()
    if text.lower().startswith("file:///"):
        text = text[8:]
    text = os.path.expandvars(os.path.expanduser(text))
    if not text:
        raise IntakeError("The message contains no path.")
    return Path(text)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def split_tags(text: str) -> list[str]:
    tags: list[str] = []
    for part in re.split(r"[,;\n]", text):
        tag = part.strip().strip("\"'").lstrip("#").strip()
        if tag and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    return tags


def parse_meta(text: str) -> dict:
    """Sections start at a 'Key:' line; a '---' line switches to the description."""
    sections: dict[str, list[str]] = {}
    current = None
    loose: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip() == "---":
            current = "description"
            sections.setdefault(current, [])
            continue
        m = HEADER.match(line)
        if m:
            current = KEY_OF[(m.group(1) or m.group(3)).lower()]
            sections.setdefault(current, [])
            if m.group(2) and m.group(2).strip():
                sections[current].append(m.group(2).strip())
            continue
        (sections[current] if current else loose).append(line)

    def block(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    description = block("description")
    loose_text = "\n".join(loose).strip()
    if not description and loose_text:
        description = loose_text  # no keys at all, or free text above the first key
    return {
        "title": " ".join(block("title").split()),
        "description": description,
        "tags": split_tags(block("tags")),
        "channel": block("channel").split()[0].lower() if block("channel") else "",
        "privacy": " ".join(block("privacy").split()),
        "schedule": " ".join(block("schedule").split()),
        "related": " ".join(block("related").split()),
        "instructions": block("instructions")[:INSTRUCTIONS_MAX_CHARS],
    }


def pick_video(target: Path) -> tuple[Path, Path]:
    if target.is_file():
        if target.suffix.lower() not in VIDEO_EXT:
            raise IntakeError(f"{target.name} is not a video ({', '.join(sorted(VIDEO_EXT))}).")
        return target, target.parent
    videos = sorted(p for p in target.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT)
    if not videos:
        raise IntakeError(f"No video in {target} ({', '.join(sorted(VIDEO_EXT))}).")
    if len(videos) > 1:
        names = ", ".join(v.name for v in videos)
        raise IntakeError(f"Several videos in the folder ({names}). Send the path of the video itself.")
    return videos[0], target


def pick_meta(folder: Path, video: Path) -> Path | None:
    texts = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in TEXT_EXT)
    for candidate in texts:
        if candidate.stem.lower() == video.stem.lower():
            return candidate
    named = [p for p in texts if p.stem.lower() in META_NAMES]
    if len(named) == 1:
        return named[0]
    if len(texts) == 1:
        return texts[0]
    if texts:
        names = ", ".join(p.name for p in texts)
        raise IntakeError(f"Several text files ({names}). Name the one with title and tags meta.txt.")
    return None


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def resolve_channel(requested: str, folder: Path, channels_dir: Path) -> tuple[str, str]:
    known = {p.stem.lower(): p.stem for p in channels_dir.glob("*.json")}
    if requested:
        if requested in known:
            return known[requested], "text file"
        raise IntakeError(f"Unknown channel '{requested}'. Known: {', '.join(sorted(known.values()))}.")
    if folder.name.lower() in known:
        return known[folder.name.lower()], "folder name"
    default = os.environ.get("DEFAULT_CHANNEL", "").strip().lower()
    if default in known:
        return known[default], "DEFAULT_CHANNEL"
    if len(known) == 1:
        return next(iter(known.values())), "only channel"
    raise IntakeError("No channel given. Add 'Channel: <name>' to the text file or set DEFAULT_CHANNEL in .env.")


def stage(src: Path, dest: Path) -> str:
    """Hard link when possible (instant, no extra space), else copy."""
    try:
        os.link(src, dest)
        return "linked"
    except OSError:
        shutil.copy2(src, dest)
        return "copied"


def stage_thumbnail(src: Path, dest_stem: Path, warnings: list[str]) -> Path:
    if src.stat().st_size <= THUMB_MAX_BYTES:
        dest = dest_stem.with_suffix(src.suffix.lower())
        stage(src, dest)
        return dest
    from PIL import Image  # only needed for oversized images

    dest = dest_stem.with_suffix(".jpg")
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((1920, 1080))
        for quality in (90, 80, 70, 60):
            im.save(dest, "JPEG", quality=quality, optimize=True)
            if dest.stat().st_size <= THUMB_MAX_BYTES:
                break
    warnings.append(f"{src.name} was over 2 MB and was re-saved as JPEG for YouTube.")
    return dest


def prune_work(work: Path) -> None:
    cutoff = time.time() - WORK_KEEP_SECONDS
    for old in work.glob("tg-*"):
        try:
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old)
        except OSError:
            pass


def resolve_visibility(msg_text: str, meta: dict, warnings: list[str], now: datetime | None = None,
                       msg_source: str = "message") -> dict:
    """Visibility and publish date: the message decides if it says anything, then the text file,
    then DEFAULT_PRIVACY. A date that is unclear, past or too close means private, never public."""
    now = now or datetime.now()
    msg = parse_options(msg_text, now)
    if msg.unknown:
        warnings.append(f"Not understood after the path, ignored: {' '.join(msg.unknown)}")
    fil = parse_options(" ".join(x for x in (meta.get("privacy"), meta.get("schedule")) if x), now)
    if fil.unknown:
        warnings.append(f"Privacy/Publish in the text file not understood: {' '.join(fil.unknown)} "
                        "(private, unlisted, public, or a date and time)")
    out = {"privacy": "private", "privacy_source": "default", "publish_at": None, "publish_at_local": None}
    chosen, source = (msg, msg_source) if msg.found else (fil, "text file") if fil.found else (None, None)
    if chosen is msg and fil.found and (fil.schedule or fil.privacy != msg.privacy):
        warnings.append(f"The {msg_source} decides the visibility; Privacy/Publish in the text file ignored.")

    if chosen and chosen.schedule:
        problem = chosen.problem
        if not problem and chosen.when < now + MIN_LEAD:
            problem = f"{show_time(chosen.when)} is in the past or less than 15 minutes away"
        if problem:
            warnings.append(f"{problem}. Not scheduled, uploading as private.")
            return {**out, "privacy_source": source}
        if chosen.privacy == "unlisted":
            warnings.append("YouTube schedules only public videos, so it goes public at the set time.")
        return {"privacy": "public", "privacy_source": source,
                "publish_at": chosen.when.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "publish_at_local": show_time(chosen.when)}
    if chosen:
        return {**out, "privacy": chosen.privacy, "privacy_source": source}
    default = os.environ.get("DEFAULT_PRIVACY", "").strip()
    if default:
        if privacy_of(default):
            return {**out, "privacy": privacy_of(default), "privacy_source": "DEFAULT_PRIVACY"}
        warnings.append(f"DEFAULT_PRIVACY '{default}' not understood; using private.")
    return out


def related_of(text: str) -> str | None:
    """A video id, 'none', or None (nothing given). Accepts a YouTube link, a bare id or 'none'."""
    text = str(text or "").strip()
    if not text:
        return None
    if YT_ID.match(text) and text.lower() not in NO_LINK:
        return text
    return parse_options(text, datetime.now()).related


def resolve_related(msg_text: str, meta: dict, overrides: dict | None, is_short: bool,
                    warnings: list[str]) -> dict:
    """Which video to link at the top of the description: app field > message > text file >
    DEFAULT_RELATED (latest long video, Shorts only)."""
    msg_source = "app" if overrides is not None else "message"
    # (value, label, is a field of its own: a bare id or 'none' counts, anything else is a mistake)
    for value, source, own_field in (((overrides or {}).get("related"), "app", True),
                                     (msg_text, msg_source, False),
                                     (meta.get("related"), "text file", True)):
        if value is None or not str(value).strip():
            continue
        rel = related_of(value) if own_field else parse_options(value, datetime.now()).related
        if rel == "none":
            return {"mode": "none", "video_id": None, "source": source}
        if rel:
            return {"mode": "video", "video_id": rel, "source": source}
        if own_field:
            warnings.append(f"Linked video '{value}' ({source}) is not a YouTube link or id; ignored.")
    default = (os.environ.get("DEFAULT_RELATED", "latest").strip().lower() or "latest")
    if default not in ("latest", "none"):
        warnings.append(f"DEFAULT_RELATED '{default}' not understood (latest or none); using latest.")
        default = "latest"
    if is_short and default == "latest":
        return {"mode": "latest", "video_id": None, "source": "Shorts default"}
    return {"mode": "none", "video_id": None, "source": "default" if is_short else "long-video default"}


def intake(target: Path, repo: Path, options_text: str = "", overrides: dict | None = None) -> dict:
    root = os.environ.get("INTAKE_ROOT", "").strip()
    if not target.exists():
        raise IntakeError(f"Path not found: {target}")
    target = target.resolve()
    if root and not target.is_relative_to(Path(os.path.expandvars(root)).resolve()):
        raise IntakeError(f"Only paths inside {root} are accepted (INTAKE_ROOT).")

    warnings: list[str] = []
    video, folder = pick_video(target)
    meta_file = pick_meta(folder, video)
    meta = parse_meta(read_text(meta_file)) if meta_file else parse_meta("")
    if meta_file:
        log(f"read {meta_file.name}")
    images = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT), key=natural_key)
    if len(images) > 2:
        warnings.append(f"{len(images)} images in the folder; used {images[0].name} (A) and {images[1].name} (B).")

    # fields typed in the desktop app win over the text file; still empty -> the LLM writes them
    field_source = {}
    for key in ("title", "description", "tags", "instructions", "channel"):
        value = (overrides or {}).get(key)
        if key == "tags" and isinstance(value, str):
            value = split_tags(value)
        if value:
            meta[key] = value[:INSTRUCTIONS_MAX_CHARS] if key == "instructions" else value
            field_source[key] = "app"
        elif meta.get(key):
            field_source[key] = "file"
    if field_source.get("channel") == "app":
        meta["channel"] = str(meta["channel"]).split()[0].lower()

    channel, channel_source = resolve_channel(meta["channel"], folder, repo / "channels")
    vis = resolve_visibility(options_text, meta, warnings, msg_source="app" if overrides is not None else "message")
    media = probe(video)
    related = resolve_related(options_text, meta, overrides, bool(media.get("is_short")), warnings)

    work = repo / "work"
    work.mkdir(exist_ok=True)
    prune_work(work)
    batch = work / f"tg-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    batch.mkdir()
    # a fixed name: the path later goes on a command line, and file names may contain % or &
    staged_video = batch / f"video{video.suffix.lower()}"
    how = stage(video, staged_video)
    log(f"{how} {video.name} into {batch.name}")
    thumbs = []
    for label, img in zip("AB", images[:2]):
        staged = stage_thumbnail(img, batch / f"thumb_{label}", warnings)
        thumbs.append({"label": label, "name": img.name, "path": staged.as_posix()})

    if meta["title"] and len(meta["title"]) > 100:
        warnings.append(f"Title has {len(meta['title'])} characters; YouTube allows 100, it will be cut.")
    missing = [k for k in ("title", "description", "tags") if not meta[k]]
    return {
        "ok": True,
        "source": target.as_posix(),
        "folder": folder.as_posix(),
        "video_source": video.as_posix(),
        "video_path": staged_video.as_posix(),
        "file_name": video.name,
        "base_name": video.stem,
        "size_mb": round(video.stat().st_size / 1e6, 1),
        **media,
        "channel": channel,
        "channel_source": channel_source,
        **vis,
        "field_source": field_source,
        "related": related,
        "meta_file": meta_file.name if meta_file else None,
        "title": meta["title"] or None,
        "description": meta["description"] or None,
        "tags": meta["tags"],
        "instructions": meta["instructions"] or None,
        "missing": missing,
        "thumbnails": thumbs,
        "warnings": warnings,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--path-hex", help="the chat message (path + optional visibility/date), UTF-8 hex-encoded")
    src.add_argument("--path", help="the same as text (for testing by hand)")
    src.add_argument("--job-hex", help="desktop app job as UTF-8 hex JSON: path, options, title, description, "
                                       "tags, instructions, channel, related")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()

    try:
        repo = Path(args.repo).resolve()
        if args.job_hex:
            job = json.loads(bytes.fromhex(args.job_hex).decode("utf-8"))
            if not isinstance(job, dict):
                raise IntakeError("The job is not a JSON object.")
            overrides = {k: job.get(k) for k in ("title", "description", "tags", "instructions", "channel",
                                                  "related")}
            result = intake(clean_path(str(job.get("path") or "")), repo, str(job.get("options") or ""), overrides)
        else:
            raw = bytes.fromhex(args.path_hex).decode("utf-8") if args.path_hex else args.path
            path_text, options_text = split_message(raw)
            result = intake(clean_path(path_text), repo, options_text)
    except IntakeError as e:
        result = {"ok": False, "error": str(e)}
    except Exception as e:  # unexpected: report it to the chat instead of a bare stack trace
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0  # the JSON carries the outcome; a non-zero exit would stop n8n before it can reply


if __name__ == "__main__":
    sys.exit(main())
