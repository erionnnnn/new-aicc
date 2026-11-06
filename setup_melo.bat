@echo off
rem MeloTTS(CPU 한국어 음성) 설치
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_melo.ps1
pause
