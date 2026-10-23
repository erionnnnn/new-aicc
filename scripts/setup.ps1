# Windows 처음 설치(PowerShell): Python 3.11+ 와 Node.js 20+ 필요
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
# 의존성 바이너리(wheel)가 확실한 Python 3.13 → 3.12 → 3.11 순으로 찾는다(3.14 이상은 빌드 도구가 필요할 수 있음)
$pyver = $null
foreach ($v in "3.13", "3.12", "3.11") { py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $pyver = $v; break } }
if (-not $pyver) { Write-Host "Python 3.13(또는 3.12/3.11)이 필요합니다. https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요."; exit 1 }
Write-Host "Python $pyver 사용"
if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
py -$pyver -m venv .venv
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
