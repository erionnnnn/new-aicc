@echo off
rem Double-click to update (git pull) and install
cd /d "%~dp0"
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
pause
