"""Desktop app for the YouTube upload workflow.

A small local server (127.0.0.1 only) and one page, opened as an Edge app window. The page takes
the folder, title, description, tags, LLM instructions, linked video and visibility or publish date,
with YouTube's limits, and shows the n8n workflow's steps live while it runs. Each job runs the
workflow once with `n8n execute` (no n8n server needed): the job goes in DESKTOP_JOB, the step
progress comes from n8n's debug log. The server exits when the window is closed.

    python app/server.py                 # open the window (the desktop shortcut uses pythonw)
    python app/server.py --no-window     # only serve http://127.0.0.1:8770
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

APP = Path(__file__).resolve().parent
REPO = APP.parent
sys.path.insert(0, str(REPO / "scripts"))
import telegram_check as tc  # noqa: E402  (load_env, n8n_command, n8n_env)

LOG = REPO / "logs" / "desktop_app.log"
RUN_OUT = REPO / "logs" / "last_desktop_run.out"
PREFS = REPO / "work" / "desktop_app.json"
APP_ID = "n8n-yt-publish-desktop"
TOKEN = secrets.token_urlsafe(24)  # every API call must send it: a web page elsewhere cannot
IDLE_EXIT_S = 15 * 60              # no window has asked anything for this long -> exit
RUN_TIMEOUT_S = 40 * 60
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The workflow's nodes, grouped into the steps the page shows. "work" is the node that means the
# step really ran (an IF in front of a skipped branch does not count).
STEPS = [
    {"key": "check", "label": "Check folder", "hint": "intake.py: video, text file, thumbnails",
     "nodes": ["Started by the desktop app", "Read job", "Is it a path?", "Check folder (intake.py)", "Parse intake",
               "Folder OK?", "Status (shown in the app)", "Problem (shown in the app)", "Not a path"],
     "work": "Parse intake"},
    {"key": "channel", "label": "Channel settings", "hint": "channels/<id>.json",
     "nodes": ["Load channel config", "Parse channel config"], "work": "Parse channel config"},
    {"key": "link", "label": "Linked video", "hint": "latest long video via the YouTube API",
     "nodes": ["Link a video?", "Find uploads playlist", "Latest uploads", "Video details", "Pick linked video"],
     "work": "Pick linked video"},
    {"key": "transcribe", "label": "Transcribe", "hint": "faster-whisper, only if text is missing",
     "nodes": ["Anything missing?", "Transcribe (faster-whisper)", "Parse transcript"], "work": "Parse transcript"},
    {"key": "llm", "label": "Write missing text", "hint": "local LLM (Ollama)",
     "nodes": ["Write missing metadata (local LLM)", "Ollama (local)", "Metadata schema"],
     "work": "Write missing metadata (local LLM)"},
    {"key": "final", "label": "Final metadata", "hint": "limits, footer, tags, link",
     "nodes": ["Final metadata"], "work": "Final metadata"},
    {"key": "upload", "label": "Upload (private)", "hint": "YouTube Data API",
     "nodes": ["Read video file", "Upload to YouTube (private)"], "work": "Upload to YouTube (private)",
     "upload_only": True},
    {"key": "thumb", "label": "Thumbnail", "hint": "thumbnail A",
     "nodes": ["Thumbnail A?", "Read thumbnail A", "Set thumbnail A"], "work": "Set thumbnail A", "upload_only": True},
    {"key": "visibility", "label": "Visibility / schedule", "hint": "public, unlisted or publish date",
     "nodes": ["Make it visible?", "Set visibility"], "work": "Set visibility", "upload_only": True},
    {"key": "log", "label": "Reply and log", "hint": "logs/runs.csv",
     "nodes": ["Build reply", "Result (shown in the app)", "Build log line", "To CSV text", "Append to logs/runs.csv"],
     "work": "Append to logs/runs.csv", "upload_only": True},
]
STEP_OF = {n: s["key"] for s in STEPS for n in s["nodes"]}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
NODE_EVENT = re.compile(r'(Start executing node|Running node) "([^"]+)"(?: (started|finished successfully|finished with error))?')


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_prefs() -> dict:
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_prefs(**kw) -> None:
    prefs = {**load_prefs(), **kw}
    PREFS.parent.mkdir(exist_ok=True)
    PREFS.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")


def channels() -> list[dict]:
    out = []
    for p in sorted((REPO / "channels").glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": p.stem, "name": data.get("display_name") or p.stem,
                    "language": data.get("metadata_language", "")})
    return out


def hexjson(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8").hex()


def clean_job(raw: dict) -> dict:
    """Only the known fields, as strings, with the same limits the page shows."""
    limits = {"path": 1000, "options": 200, "title": 100, "description": 5000, "tags": 600,
              "instructions": 2000, "channel": 60, "related": 200}
    job = {}
    for key, limit in limits.items():
        value = raw.get(key)
        if value is None:
            continue
        value = str(value)
        if key == "description":
            while len(value.encode("utf-8")) > 5000:
                value = value[:-1]
        job[key] = value[:limit] if key != "description" else value
    return job


def run_intake(job: dict) -> dict:
    """The quick folder check, without n8n (the same script the workflow runs first)."""
    py = os.environ.get("PYTHON") or sys.executable.replace("pythonw.exe", "python.exe")
    p = subprocess.run([py, str(REPO / "scripts" / "intake.py"), "--job-hex", hexjson(job), "--repo", str(REPO)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       creationflags=NO_WINDOW, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"ok": False, "error": f"intake.py gave no result (exit {p.returncode}): {p.stderr[-300:]}"}


class Run:
    """One n8n execution and what the page needs to draw it."""

    def __init__(self, mode: str, job: dict):
        self.id = int(time.time() * 1000)
        self.mode, self.job = mode, job
        self.workflow = (os.environ.get("DESKTOP_PREVIEW_WORKFLOW_ID", "YtDesktopPreview01") if mode == "preview"
                         else os.environ.get("DESKTOP_WORKFLOW_ID", "YtDesktopUpload01"))
        self.status = "running"
        self.started, self.finished = time.time(), None
        self.nodes: dict[str, dict] = {}
        self.lines: list[str] = []
        self.result: dict = {}
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cancelled = False

    # -- n8n output
    def on_line(self, line: str) -> None:
        line = ANSI.sub("", line)
        m = NODE_EVENT.search(line)
        now = time.time()
        if m:
            name, what = m[2], m[3] or "start"
            n = self.nodes.setdefault(name, {"status": "running", "start": now, "end": None})
            if what in ("start", "started"):
                n.update(status="running")
            elif what == "finished successfully":
                n.update(status="done", end=now)
            else:
                n.update(status="error", end=now)
            if what != "start":  # "Start executing" and "started" come in pairs
                word = {"started": "started", "finished successfully": "done"}.get(what, "ERROR")
                took = f" ({now - n['start']:.1f} s)" if word != "started" and n.get("start") else ""
                self.lines.append(f"{time.strftime('%H:%M:%S')}  {name}: {word}{took}")
        elif re.search(r"\b(error|Error|ERROR)\b", line) and not line.lstrip().startswith(('"', "{", "}")):
            self.lines.append(f"{time.strftime('%H:%M:%S')}  {line.strip()[:240]}")
        del self.lines[:-80]

    def execute(self) -> None:
        env = tc.n8n_env({})
        env.pop("TG_MESSAGE", None)
        env.update({"DESKTOP_JOB": json.dumps(self.job, ensure_ascii=False), "N8N_LOG_LEVEL": "debug",
                    "N8N_LOG_OUTPUT": "console", "N8N_RUNNERS_BROKER_PORT": "5691", "NO_COLOR": "1"})
        out: list[str] = []
        try:
            self.proc = subprocess.Popen([tc.n8n_command(), "execute", f"--id={self.workflow}"], env=env, cwd=REPO,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            timer = threading.Timer(RUN_TIMEOUT_S, self.cancel)
            timer.start()
            for line in self.proc.stdout:
                out.append(line)
                self.on_line(line)
            code = self.proc.wait()
            timer.cancel()
        except Exception as e:  # n8n missing, ...
            self.error, self.status, self.finished = f"Could not start n8n: {e}", "error", time.time()
            log(self.error)
            return
        text = "".join(out)
        RUN_OUT.write_text(text, encoding="utf-8", errors="replace")
        try:
            self.finish(code, text)
        except Exception as e:  # never leave the page waiting on "running"
            self.status, self.error, self.finished = "error", f"Could not read n8n's result: {e}", time.time()
            log(self.error)

    def finish(self, code: int, text: str) -> None:
        # the execution JSON follows "Execution was (not) successful"; in debug mode its first line
        # carries the log prefix ("22:23:55.361   info    {") and colour codes
        text = ANSI.sub("", text)
        data = None
        marker = text.rfind("Execution was")
        start = re.compile(r"^(?:[\d:.]+\s+\w+\s+)?\{\s*$", re.MULTILINE).search(text, max(marker, 0))
        if start:
            try:
                data, _ = json.JSONDecoder().raw_decode(text[start.start() + start[0].index("{"):])
            except ValueError:
                data = None
        run_data = ((data or {}).get("data") or {}).get("resultData", {}).get("runData", {}) if data else {}

        def first(name):
            try:
                return run_data[name][0]["data"]["main"][0][0]["json"]
            except (KeyError, IndexError, TypeError):
                return None

        intake = first("Parse intake") or {}
        final = first("Final metadata")
        reply = first("Build reply")
        for name, runs in run_data.items():  # fill in nodes the log did not show
            r0 = runs[0] if runs else {}
            st = "error" if r0.get("error") else "done"
            n = self.nodes.setdefault(name, {"status": st, "start": None, "end": None})
            if n["status"] == "running":
                n["status"] = st
        self.result = {
            "intake": {k: intake.get(k) for k in ("ok", "error", "file_name", "is_short", "duration_s", "channel",
                                                  "privacy", "privacy_source", "publish_at_local", "related",
                                                  "missing", "warnings", "thumbnails",
                                                  "meta_file")} if intake else None,
            "final": final,
            "reply": {"text": re.sub(r"<[^>]+>", "", reply.get("text", "")).replace("&lt;", "<").replace("&gt;", ">")
                      .replace("&amp;", "&"), "videoId": reply.get("videoId"), "outcome": reply.get("outcome")}
            if reply else None,
        }
        err = ((data or {}).get("data") or {}).get("resultData", {}).get("error") if data else None
        if self.cancelled:
            self.status, self.error = "cancelled", "Stopped."
        elif intake and intake.get("ok") is False:
            self.status, self.error = "error", intake.get("error")
        elif "Not a path" in run_data:
            self.status, self.error = "error", "That is not a folder path."
        elif err or code != 0 or not data:
            self.status = "error"
            self.error = (err or {}).get("message") if err else f"n8n stopped with exit code {code}; see logs/{RUN_OUT.name}"
        else:
            self.status = "done"
        self.finished = time.time()
        log(f"run {self.id} ({self.mode}) {self.status} after {self.finished - self.started:.0f} s"
            + (f": {self.error}" if self.error else ""))

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.cancelled = True
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)

    # -- what the page draws
    def steps(self) -> list[dict]:
        out, now = [], time.time()
        last_started = max((i for i, s in enumerate(STEPS) if any(n in self.nodes for n in s["nodes"])), default=-1)
        for i, s in enumerate(STEPS):
            seen = [self.nodes[n] for n in s["nodes"] if n in self.nodes]
            starts = [n["start"] for n in seen if n["start"]]
            ends = [n["end"] or now for n in seen if n["start"]]
            if s.get("upload_only") and self.mode == "preview":
                status = "off"
            elif any(n["status"] == "error" for n in seen):
                status = "error"
            elif any(n["status"] == "running" for n in seen) and self.status == "running":
                status = "running"
            elif s["work"] in self.nodes:
                status = "done"
            elif i < last_started or (self.status != "running" and seen):
                status = "skipped"
            else:
                status = "waiting" if self.status == "running" else "not run"
            out.append({"key": s["key"], "label": s["label"], "hint": s["hint"], "status": status,
                        "seconds": round(max(ends) - min(starts), 1) if starts else None})
        return out

    def state(self) -> dict:
        return {"id": self.id, "mode": self.mode, "status": self.status, "error": self.error,
                "elapsed": round((self.finished or time.time()) - self.started), "steps": self.steps(),
                "lines": self.lines[-40:], "result": self.result if self.status != "running" else None}


class App:
    def __init__(self):
        self.run: Run | None = None
        self.lock = threading.Lock()
        self.dialog_lock = threading.Lock()
        self.last_seen = time.time()
        self.bye_at: float | None = None

    def start(self, mode: str, job: dict) -> tuple[bool, str]:
        with self.lock:
            if self.run and self.run.status == "running":
                return False, "A run is still going."
            self.run = Run(mode, job)
            threading.Thread(target=self.run.execute, daemon=True).start()
        if job.get("path"):
            save_prefs(last_folder=str(Path(job["path"]).parent))
        log(f"run {self.run.id} ({mode}) started: {job.get('path')}")
        return True, ""

    def pick_folder(self) -> str:
        with self.dialog_lock:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                initial = load_prefs().get("last_folder") or os.environ.get("INTAKE_ROOT") or str(Path.home())
                path = filedialog.askdirectory(parent=root, initialdir=initial, mustexist=True,
                                               title="Folder with the video")
            finally:
                root.destroy()
            return str(Path(path)) if path else ""

    def watchdog(self, server: ThreadingHTTPServer) -> None:
        while True:
            time.sleep(2)
            busy = self.run is not None and self.run.status == "running"
            if busy:
                continue
            if (self.bye_at and time.time() - self.bye_at > 4 and self.last_seen < self.bye_at) \
                    or time.time() - self.last_seen > IDLE_EXIT_S:
                log("window closed, exiting")
                server.shutdown()
                return


APP_STATE = App()


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-desktop"

    def log_message(self, *args):  # quiet
        pass

    def send(self, code: int, body: bytes | str | dict, ctype: str = "application/json") -> None:
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self) -> bool:
        """Only this machine, under our own host name (no DNS rebinding), and API calls with the token."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
            return False
        if self.path.startswith("/api/") and self.path not in ("/api/ping", "/api/bye") \
                and self.headers.get("X-App-Token") != TOKEN:
            self.send(HTTPStatus.FORBIDDEN, {"error": "missing token"})
            return False
        if self.path != "/api/bye":  # the goodbye itself is not a sign of life
            APP_STATE.last_seen = time.time()
        return True

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):
        if not self.allowed():
            return
        if self.path in ("/", "/index.html"):
            html = (APP / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", TOKEN)
            return self.send(200, html, "text/html")
        if self.path == "/api/ping":
            return self.send(200, {"app": APP_ID})
        if self.path == "/api/info":
            env = os.environ
            return self.send(200, {
                "channels": channels(), "default_channel": env.get("DEFAULT_CHANNEL", ""),
                "default_privacy": env.get("DEFAULT_PRIVACY", "private") or "private",
                "default_related": env.get("DEFAULT_RELATED", "latest") or "latest",
                "steps": [{k: s[k] for k in ("key", "label", "hint")} | {"upload_only": bool(s.get("upload_only"))}
                          for s in STEPS],
                "last_folder": load_prefs().get("last_folder", ""),
            })
        if self.path == "/api/state":
            run = APP_STATE.run
            return self.send(200, run.state() if run else {"status": "idle"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return
        if self.path == "/api/bye":
            APP_STATE.bye_at = time.time()
            return self.send(200, {"ok": True})
        data = self.body()
        if self.path == "/api/pick-folder":
            return self.send(200, {"path": APP_STATE.pick_folder()})
        if self.path == "/api/check":
            job = clean_job(data.get("job") or {})
            return self.send(200, run_intake(job))
        if self.path == "/api/run":
            mode = "preview" if data.get("mode") == "preview" else "upload"
            job = clean_job(data.get("job") or {})
            if not job.get("path"):
                return self.send(400, {"ok": False, "error": "No folder chosen."})
            ok, err = APP_STATE.start(mode, job)
            return self.send(200 if ok else 409, {"ok": ok, "error": err})
        if self.path == "/api/cancel":
            if APP_STATE.run:
                APP_STATE.run.cancel()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def find_edge() -> str | None:
    for cand in (shutil.which("msedge"),
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


def open_window(url: str) -> None:
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1320,900"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)


def already_running(port: int) -> bool:
    try:
        with urlopen(Request(f"http://127.0.0.1:{port}/api/ping"), timeout=1) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except (URLError, OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("DESKTOP_APP_PORT", "8770")))
    ap.add_argument("--no-window", action="store_true", help="serve only, do not open a window")
    args = ap.parse_args()
    tc.load_env()
    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):  # second double-click: show the window of the running app
        if not args.no_window:
            open_window(url)
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=APP_STATE.watchdog, args=(server,), daemon=True).start()
    log(f"serving {url}")
    if sys.stdout:
        print(f"YouTube upload app on {url} (closes with its window)", flush=True)
    if not args.no_window:
        open_window(url)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
