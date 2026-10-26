@echo off
rem 더블클릭으로 설치/업데이트: 최신 코드 받기 + 설치 스크립트 실행
cd /d "%~dp0"
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
pause
