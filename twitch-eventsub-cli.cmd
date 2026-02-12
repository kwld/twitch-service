@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0twitch-eventsub-cli.ps1" %*
exit /b %ERRORLEVEL%
