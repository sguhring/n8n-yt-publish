@echo off
rem Processes the folder paths waiting in the Telegram bot: n8n runs once per path, then exits.
rem The desktop shortcut "YouTube upload (Telegram)" points here (setup\create-shortcut.ps1).
title YouTube upload from Telegram
cd /d "%~dp0"
python scripts\telegram_check.py %*
echo.
echo Done. Progress and links are in Telegram. This window closes in 20 seconds.
timeout /t 20 >nul
