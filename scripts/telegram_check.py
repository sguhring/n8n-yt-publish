"""Fetch the folder paths waiting in the Telegram bot and run the n8n workflow once per path.

Started by the desktop shortcut "YouTube upload (Telegram)" (setup/create-shortcut.ps1), so
nothing runs in the background: send the bot one or more paths, then double-click the
shortcut. For each message it tells the bot it is starting, runs
`n8n execute --id YtTelegramIntake01` with the message as JSON in TG_MESSAGE (n8n starts,
runs the folder check, metadata, private upload and Telegram reply, and exits again) and
reports a failed run to the bot. Telegram keeps undelivered messages for 24 hours.

Needs in .env: TELEGRAM_BOT_TOKEN (the bot's token, the same one as in n8n's Telegram
credential) and TELEGRAM_CHAT_ID. Messages from any other chat are dropped.

Usage:
    python scripts/telegram_check.py            # one check (what the shortcut runs)
    python scripts/telegram_check.py --dry-run  # show waiting messages, run and confirm nothing

Files: logs/telegram_check.log (one line per event, quiet while idle),
       work/telegram_check.last (time of the last check, for the dashboard),
       logs/last_n8n_run.out (n8n's output of the most recent run).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "logs" / "telegram_check.log"
LAST = REPO / "work" / "telegram_check.last"
RUN_OUT = REPO / "logs" / "last_n8n_run.out"
LOCK = REPO / "work" / "telegram_check.lock"
RUN_TIMEOUT_S = 30 * 60
LOG_MAX_BYTES = 1_000_000


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if sys.stdout:  # pythonw (scheduled task) has no console
        print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
        LOG.replace(LOG.with_suffix(".log.1"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env() -> None:
    """KEY=VALUE lines from .env; variables already set (e.g. by a test) win."""
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.isidentifier() and key not in os.environ:
            os.environ[key] = value.strip().strip('"')


def acquire_lock():
    """Only one check at a time; the lock goes away with the process."""
    LOCK.parent.mkdir(exist_ok=True)
    f = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class Telegram:
    def __init__(self, token: str, base: str):
        self.url = f"{base.rstrip('/')}/bot{token}/"

    def call(self, method: str, **params):
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
        ).encode()
        req = urllib.request.Request(self.url + method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"ok": False, "error_code": e.code, "description": str(e)}
        if not body.get("ok"):
            raise TelegramError(body.get("error_code"), body.get("description", "unknown error"))
        return body["result"]


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code


def n8n_server_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5678/healthz", timeout=2):
            return True
    except Exception:
        return False


def fetch_updates(tg: Telegram) -> list[dict]:
    try:
        return tg.call("getUpdates", timeout=0, allowed_updates=["message"])
    except TelegramError as e:
        if e.code != 409:
            raise
        # 409: a webhook is set, e.g. left over from the n8n server with a Telegram Trigger.
        # While that server runs it owns the bot; otherwise remove the stale webhook.
        if n8n_server_running():
            log("n8n server is running and owns the bot's webhook; skipped this check")
            return []
        tg.call("deleteWebhook", drop_pending_updates=False)
        log("removed a stale Telegram webhook so messages can be fetched")
        return tg.call("getUpdates", timeout=0, allowed_updates=["message"])


def n8n_command() -> str:
    for candidate in (os.environ.get("N8N_CMD"), shutil.which("n8n"),
                      str(Path(os.environ.get("APPDATA", "")) / "npm" / "n8n.cmd")):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("n8n not found (npm install -g n8n, or set N8N_CMD in .env)")


def n8n_env(message: dict) -> dict:
    """Same settings as start-n8n.ps1, plus the message for the workflow."""
    env = dict(os.environ)
    repo = REPO.as_posix()
    env.update({
        "N8N_YT_REPO": repo,
        "NODES_EXCLUDE": "[]",
        "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false",
        "N8N_RESTRICT_FILE_ACCESS_TO": str(REPO),
        "N8N_DEFAULT_BINARY_DATA_MODE": "filesystem",
        "N8N_DIAGNOSTICS_ENABLED": "false",
        # own task-runner port, so a run also works while an n8n server is open on 5679
        "N8N_RUNNERS_BROKER_PORT": env.get("N8N_RUNNERS_BROKER_PORT", "5690"),
        "TG_MESSAGE": json.dumps(message, ensure_ascii=False),
    })
    env.setdefault("GENERIC_TIMEZONE", "Europe/Lisbon")
    env.pop("TELEGRAM_BOT_TOKEN", None)  # n8n uses its own credential store
    return env


def run_workflow(message: dict, workflow_id: str) -> int:
    cmd = [n8n_command(), "execute", f"--id={workflow_id}"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    t0 = time.time()
    with RUN_OUT.open("w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.run(cmd, env=n8n_env(message), stdout=out, stderr=subprocess.STDOUT,
                              cwd=REPO, timeout=RUN_TIMEOUT_S, creationflags=flags)
    log(f"n8n run for message {message.get('message_id')} finished with exit {proc.returncode} "
        f"after {round(time.time() - t0)} s (output in logs/{RUN_OUT.name})")
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list waiting messages only")
    args = ap.parse_args()
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")

    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env; nothing checked")
        return 2

    lock = acquire_lock()
    if lock is None:
        return 0  # the previous check is still running a workflow

    tg = Telegram(token, os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org"))
    workflow_id = os.environ.get("TELEGRAM_WORKFLOW_ID", "YtTelegramIntake01")
    try:
        updates = fetch_updates(tg)
    except Exception as e:  # offline, Telegram down: try again next minute
        log(f"could not reach Telegram: {e}")
        return 1
    finally:
        LAST.parent.mkdir(exist_ok=True)
        LAST.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")

    if not updates:
        log("no paths waiting in Telegram")
        return 0
    messages = [u["message"] for u in updates
                if "message" in u and str(u["message"].get("chat", {}).get("id")) == chat_id]
    dropped = len(updates) - len(messages)
    if args.dry_run:
        for m in messages:
            print(f"waiting: message {m.get('message_id')}: {str(m.get('text', '(no text)'))[:120]}")
        print(f"{len(messages)} message(s) for this chat, {dropped} other update(s); nothing confirmed")
        return 0

    # Confirm before running: a crash mid-run then loses one message instead of uploading twice
    tg.call("getUpdates", offset=max(u["update_id"] for u in updates) + 1, timeout=0, limit=1)
    if dropped:
        log(f"ignored {dropped} update(s) from other chats")
    if not messages:
        log("no paths waiting in Telegram")
    for i, m in enumerate(messages, 1):
        log(f"message {m.get('message_id')} ({i} of {len(messages)}): {str(m.get('text', '(no text)'))[:200]!r}")
        notify(tg, chat_id, m, f"⏳ Starting n8n ({i} of {len(messages)}), the folder check follows in about 30 s.")
        try:
            code = run_workflow(m, workflow_id)
        except Exception as e:
            log(f"n8n run failed to start: {e}")
            notify(tg, chat_id, m, f"❌ Could not start n8n: {e}")
            continue
        if code != 0:
            notify(tg, chat_id, m, f"❌ The n8n run stopped with exit code {code}. "
                                    f"Details on the PC in logs/{RUN_OUT.name}.")
    return 0


def notify(tg: Telegram, chat_id: str, message: dict, text: str) -> None:
    """Short status line to the bot as a reply to the path message; never fatal."""
    try:
        tg.call("sendMessage", chat_id=chat_id, text=text,
                reply_to_message_id=message.get("message_id"), allow_sending_without_reply=True)
    except Exception as e:
        log(f"could not send status to Telegram: {e}")


if __name__ == "__main__":
    sys.exit(main())
