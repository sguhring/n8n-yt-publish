# Starts a local n8n instance configured for this project.
# Usage (PowerShell, from the repo folder):
#   .\start-n8n.ps1            # n8n on http://localhost:5678
#   .\start-n8n.ps1 -Tunnel    # same, plus a temporary public HTTPS URL (cloudflared) so the
#                              # Telegram Publish / Keep private buttons work, also from a phone
param([switch]$Tunnel)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

# Load .env (KEY=VALUE lines) into this process so n8n can read them
$envFile = Join-Path $repo ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        Set-Item -Path "env:$($name.Trim())" -Value $value.Trim().Trim('"')
    }
} else {
    Write-Warning ".env not found - copy .env.example to .env and fill it in."
}

# Forward slashes: n8n's file nodes treat paths as glob patterns
$env:N8N_YT_REPO = $repo -replace '\\', '/'
# n8n 2.x disables Execute Command and Local File Trigger by default; this workflow needs them on a trusted local machine
$env:NODES_EXCLUDE = "[]"
# Expressions may read env vars (N8N_YT_REPO, TELEGRAM_CHAT_ID, PYTHON)
$env:N8N_BLOCK_ENV_ACCESS_IN_NODE = "false"
# File nodes may only touch this repo
$env:N8N_RESTRICT_FILE_ACCESS_TO = $repo
# Keep video files out of memory
$env:N8N_DEFAULT_BINARY_DATA_MODE = "filesystem"
if (-not $env:GENERIC_TIMEZONE) { $env:GENERIC_TIMEZONE = "Europe/Lisbon" }
$env:N8N_DIAGNOSTICS_ENABLED = "false"

New-Item -ItemType Directory -Force (Join-Path $repo "work"), (Join-Path $repo "logs") | Out-Null
foreach ($ch in Get-ChildItem (Join-Path $repo "channels") -Filter *.json) {
    New-Item -ItemType Directory -Force (Join-Path $repo "inbox\$($ch.BaseName)") | Out-Null
}
$log = Join-Path $repo "logs\runs.csv"
if (-not (Test-Path $log)) {
    Set-Content -Path $log -Encoding utf8 -Value "timestamp,channel,file,video_id,title,outcome,detail,execution_id"
}

if ($Tunnel) {
    # winget puts cloudflared in its package folder, which older shells may not have on PATH
    $cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    if (-not $cf) {
        $cf = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Cloudflare.cloudflared*\cloudflared.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $cf) {
        throw "cloudflared not found. Install it (winget install Cloudflare.cloudflared) or set WEBHOOK_URL in .env yourself."
    }
    $tunnelLog = Join-Path $repo "work\tunnel.log"
    Remove-Item $tunnelLog -ErrorAction SilentlyContinue
    $proc = Start-Process $cf -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://localhost:5678" `
        -RedirectStandardError $tunnelLog -WindowStyle Hidden -PassThru
    $url = $null
    for ($i = 0; $i -lt 30 -and -not $url; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $tunnelLog) {
            $m = Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value }
        }
    }
    if (-not $url) { Stop-Process -Id $proc.Id -ErrorAction SilentlyContinue; throw "No tunnel URL after 30 s, see $tunnelLog" }
    # Approval links and webhooks use the tunnel; the editor and Google's OAuth callback stay on localhost
    $env:WEBHOOK_URL = "$url/"
    $env:N8N_EDITOR_BASE_URL = "http://localhost:5678/"
    Write-Host "Public URL for approval links: $url (stops when this window closes)"
} elseif (-not $env:WEBHOOK_URL) {
    Write-Host "Note: without -Tunnel or WEBHOOK_URL the Telegram buttons point to localhost, which Telegram may refuse."
}

# Find n8n even when the npm global folder is not on this shell's PATH
$n8nCmd = (Get-Command n8n -ErrorAction SilentlyContinue).Source
if (-not $n8nCmd -and (Test-Path "$env:APPDATA\npm\n8n.cmd")) { $n8nCmd = "$env:APPDATA\npm\n8n.cmd" }
if (-not $n8nCmd) { throw "n8n not found. Install it with: npm install -g n8n" }

try {
    & $n8nCmd start
} finally {
    if ($proc) { Stop-Process -Id $proc.Id -ErrorAction SilentlyContinue }
}
