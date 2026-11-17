# Registers a Windows scheduled task that runs scripts/telegram_check.py every minute while you
# are logged in. Each check takes about a second and exits; n8n only starts when a folder path is
# waiting in the bot, and exits again after the run. No window opens (pythonw).
#
# Usage (PowerShell, from the repo folder):
#   .\setup\install-telegram-task.ps1            # install or update (every minute)
#   .\setup\install-telegram-task.ps1 -Minutes 2 # check every 2 minutes
#   .\setup\install-telegram-task.ps1 -Remove    # uninstall
param([int]$Minutes = 1, [switch]$Remove)

$name = "n8n YouTube - Telegram check"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task '$name'."
    return
}

$repo = Split-Path $PSScriptRoot -Parent
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw "pythonw not found on PATH (it comes with the python.org installer)." }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$repo\scripts\telegram_check.py`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$trigger.Repetition.Duration = ""   # repeat indefinitely
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Every $Minutes min: fetch folder paths sent to the Telegram bot and run the n8n upload workflow once per path ($repo)" `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $name
Write-Host "Installed task '$name': every $Minutes min, repeat for '$($t.Triggers[0].Repetition.Duration)' (empty = indefinitely), state $($t.State)."
Write-Host "Log: $repo\logs\telegram_check.log   Remove: .\setup\install-telegram-task.ps1 -Remove"
