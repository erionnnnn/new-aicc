# Windows 처음 설치(PowerShell): Python 3.11+ 와 Node.js 20+ 필요
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip | Out-Null
.\.venv\Scripts\pip install -e ".[dev]"
Push-Location apps\web; npm ci; npm run build; Pop-Location
# 바탕화면 바로가기(AICC) 생성 — 이후 이 아이콘 하나만 실행
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "AICC.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = (Resolve-Path ".\.venv\Scripts\aicc.exe").Path
$s.WorkingDirectory = (Get-Location).Path
$s.Save()
Write-Host "설치 완료. 바탕화면의 AICC 아이콘을 실행하세요."
