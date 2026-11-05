@echo off
rem VoiceGetter one-click installer for Windows. Double-click this file.
title VoiceGetter installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1"
echo.
pause
