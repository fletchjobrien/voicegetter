@echo off
rem Removes VoiceGetter from this PC.
title VoiceGetter uninstaller
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" -Uninstall
echo.
pause
