# Puts two shortcuts on the desktop:
#   "YouTube upload (Telegram)" runs upload-from-telegram.cmd (folders sent to the bot)
#   "YouTube upload (Desktop)"  opens the desktop app (app/server.py, live workflow view)
# Usage (PowerShell, from the repo folder):  .\setup\create-shortcut.ps1
$repo = Split-Path $PSScriptRoot -Parent
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

$sc = $shell.CreateShortcut((Join-Path $desktop "YouTube upload (Telegram).lnk"))
$sc.TargetPath = Join-Path $repo "upload-from-telegram.cmd"
$sc.WorkingDirectory = $repo
$sc.Description = "Upload the folders sent to the Telegram bot to YouTube"
$sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,186"
$sc.Save()
Write-Host "Shortcut created: YouTube upload (Telegram)"

$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw "pythonw not found on PATH (it comes with the python.org installer)." }
$sc = $shell.CreateShortcut((Join-Path $desktop "YouTube upload (Desktop).lnk"))
$sc.TargetPath = $pyw
$sc.Arguments = "`"$repo\app\server.py`""
$sc.WorkingDirectory = $repo
$sc.Description = "YouTube upload app: text fields with limits, live n8n workflow steps"
$sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,190"
$sc.Save()
Write-Host "Shortcut created: YouTube upload (Desktop)"
