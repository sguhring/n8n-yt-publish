# Setup

You need: Windows/macOS/Linux, Node.js 24+, Python 3.10+ with `faster-whisper` and `Pillow`, and ffmpeg on the PATH, and [Ollama](https://ollama.com) with a chat model pulled (default `gemma4:latest`: `ollama pull gemma4`).

```powershell
npm install -g n8n          # tested with n8n 2.47.7 on Node 24.16
pip install faster-whisper pillow
copy .env.example .env      # then fill in TELEGRAM_CHAT_ID
.\start-n8n.ps1             # opens n8n on http://localhost:5678
```

On first start n8n asks you to create a local owner account (stays on your machine).

## 1. Import credentials and workflows

From a terminal in the repo folder, with n8n stopped:

```powershell
n8n import:credentials --input=setup/credential-stubs.json   # Ollama (complete) plus empty Gemini, Telegram and YouTube credentials, fixed ids
n8n import:workflow --separate --input=workflows             # all workflows, already linked to those credentials
```

The stubs contain names and ids only. You fill in the secrets in n8n (next section), and the workflows pick them up without reselecting anything.
Without the CLI: **Workflows → Import from file** (error workflow first), then create the three credentials by hand and select them in the nodes.

## 2. Credentials (fill in under **Credentials** in n8n)

### Ollama (title, description, tags; runs locally)
1. The credential `Ollama (local)` is imported complete, with base URL `http://127.0.0.1:11434` and no key.
   - Use `127.0.0.1`, not `localhost`: on Windows, Node resolves `localhost` to the IPv6 address `::1`, where Ollama does not listen (`ECONNREFUSED ::1:11434`).
2. The model comes from `OLLAMA_MODEL` in `.env` (default `gemma4:latest`); restart n8n after changing it. The node turns thinking off, asks for JSON and keeps the model loaded for 10 minutes.
3. Per-video instructions: a `clip.txt` or `clip.md` next to `clip.mp4` (up to 2,000 characters, any language) is added to the prompt.

### Google Gemini (optional cloud alternative)
1. Create an API key at <https://aistudio.google.com/apikey> and paste it into the credential `Gemini` (type **Google Gemini(PaLM) Api**).
2. Add a **Google Gemini Chat Model** node with model `models/gemini-flash-latest` and connect it to "Write metadata (local LLM)" in place of "Ollama (local)". Pinned names such as `gemini-2.5-flash` are closed to new API keys.

### Telegram (preview, approval, alerts)
1. In Telegram, talk to **@BotFather** → `/newbot` → copy the bot token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `message.chat.id`. Put it in `.env` as `TELEGRAM_CHAT_ID` and restart n8n.
3. Open the credential `Telegram bot` (type **Telegram API**) and paste the token.

### YouTube (upload, thumbnail, publish)
1. <https://console.cloud.google.com> → create a project (e.g. `n8n-yt-publish`).
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **Google Auth Platform → Branding / Audience**: user type *External*, add your own Google account as a **test user**.
4. **Clients → Create client** → *Web application*. Authorised redirect URI: `http://localhost:5678/rest/oauth2-credential/callback` (n8n shows the exact URL in the credential dialog).
5. Open the credential `YouTube` (type **YouTube OAuth2 API**), paste client ID and secret, click **Sign in with Google** and pick the account or brand account that owns the channel.

If you created the credentials by hand instead of importing the stubs, select them in the Ollama, Telegram, YouTube, "Set custom thumbnail" and "Make public" nodes. In the Telegram and desktop workflows the YouTube credential is also used by "Set thumbnail A", "Set visibility", "Find uploads playlist", "Latest uploads" and "Video details".

## 3. Things Google enforces (not bugs in the workflow)

- **Uploads stay private until your API project is audited.** Videos uploaded through an API project created after 28 July 2020 that has not passed YouTube's API compliance audit are locked to private. The workflow still uploads, sets metadata and asks for approval, but the "make public" step reports the lock instead of publishing. Apply via the *YouTube API Services – Audit and Quota Extension Form* (linked from the YouTube Data API "Quota and compliance" docs).
- **Custom thumbnails** need a verified channel (<https://www.youtube.com/verify>). Without it the thumbnail step fails softly and the preview says so.
- **Quota**: the default daily quota of the YouTube Data API allows only a few uploads per day.
- **Test-mode tokens**: while the Google app is in *Testing*, the refresh token expires after 7 days; reconnect the YouTube credential or move the app to *In production* (fine for personal use).

## 4. The Publish / Keep private buttons need a reachable address

The Telegram buttons are links back to n8n (signed, single-use resume URLs). With default settings they point to `http://localhost:5678`. Telegram generally refuses `localhost` in button links, and a phone could not open it anyway. Two options:

- **Quick tunnel (recommended for a home setup):** install cloudflared once (`winget install Cloudflare.cloudflared`), then start with `.\start-n8n.ps1 -Tunnel`.
  - The script opens a temporary `https://….trycloudflare.com` address and sets it as `WEBHOOK_URL`.
  - The n8n editor and Google's OAuth callback stay on localhost.
  - The address changes on every start and closes with the window.
  - The tunnel makes n8n's login page and webhooks reachable from the internet while it runs. The editor still needs your n8n login, and approval links carry a signature.
- **Own address:** set `WEBHOOK_URL` in `.env` to a URL that reaches this machine (reverse proxy, fixed tunnel) and keep `N8N_EDITOR_BASE_URL=http://localhost:5678/`.

## 5. Use it

1. Optional dry run: put any short video at `inbox/example/sample.mp4`, open the workflow and click **Test workflow** (manual trigger). Then activate the workflow (toggle top right); the folder trigger only runs while it is active.
2. Drop a video into `inbox/<channel>/`, e.g. `inbox/example/eggs.mp4`. Optional, with the same base name: a thumbnail (`eggs.jpg`) and instructions for the LLM (`eggs.txt` or `.md`), e.g. `Start the title with "Kitchen tip:"`. Both may arrive a few seconds after the video, because they are read after transcription.
3. Wait for the Telegram preview, check it on YouTube Studio via the link, press **Approve** or **Decline**.

### Upload from Telegram with a folder path

No n8n server has to run for this. n8n starts when you double-click the shortcut, handles the paths you sent, and exits.

1. Once: paste the bot token into `.env` as `TELEGRAM_BOT_TOKEN=` (the same token as in n8n's Telegram credential), then create the desktop shortcut with `.\setup\create-shortcut.ps1`.
2. Put the video, an optional `meta.txt` (Title / Tags / Description) and up to two thumbnails in one folder.
3. In Explorer select the folder, press Ctrl+Shift+C and paste the path into the chat with your bot. Add `public` or `unlisted` after the path if the video should not stay private, or a date and time (`18.11. 18:00`, `morgen 18 Uhr`) to schedule it, or a YouTube link to name the linked video (`no link` for none). You can send several paths, also from your phone; Telegram keeps them for 24 hours.
4. Double-click **YouTube upload (Telegram)** on the desktop. For each path the bot says it is starting, then what it found in the folder, then the upload with its visibility and a Studio link. A small window shows the same and closes itself.
5. To stop n8n, send `/stop` to the bot. The shortcut's window has to be open for this: while a path runs, and for 10 minutes after the last one if n8n is still running then (`STOP_LISTEN_MINUTES`). The bot replies with what it stopped and which waiting paths it dropped. A `/stop` sent while no window is open waits in the chat. On the next double-click it drops the paths sent before it and stops any n8n that is running.

Set `DEFAULT_CHANNEL` in `.env` for folders without a `Channel:` line, and `DEFAULT_PRIVACY` (`private`, `unlisted` or `public`) for messages and text files that name no visibility. If the API project has not passed YouTube's audit, a public or unlisted request comes back as "YouTube kept it private". `INTAKE_ROOT` (optional) limits which folders are accepted.
`python scripts/telegram_check.py --dry-run` lists the waiting paths without running anything. Logs: `logs/telegram_check.log` and `logs/last_n8n_run.out`.
While an n8n server with a Telegram Trigger is running, it owns the bot and the shortcut skips the check; otherwise the shortcut removes a leftover webhook by itself.

`DEFAULT_RELATED=latest` (default) links the channel's newest public video longer than 3 minutes at the top of every Short's description; `none` turns that off. A channel file can set `related_label` (default by language, e.g. "▶ Full video").

### Desktop app

1. Once: `.\setup\create-shortcut.ps1` puts **YouTube upload (Desktop)** on the desktop (next to the Telegram one). The two desktop workflows come with the import in section 1.
2. Double-click it. A window opens (Edge app mode; another browser if Edge is missing).
3. Choose the folder, check what it found, type or edit the texts, choose visibility or a publish date. **Write missing text with AI** gives a draft without uploading; **Upload** runs the whole workflow. Each step lights up while n8n runs it.
4. Close the window when done; the app exits with it. Logs: `logs/desktop_app.log` and `logs/last_desktop_run.out`.

Add a channel by copying `channels/example.json` (a made-up demo channel) to `channels/<new-id>.json` and editing language, tags, footer, style and thumbnail colours; the inbox folder is created on the next start. Your own channel files stay out of git (`.gitignore`).
