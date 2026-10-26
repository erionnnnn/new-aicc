# Windows 처음 설치(PowerShell): Python 3.11+ 와 Node.js 20+ 필요
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
# 의존성 바이너리(wheel)가 확실한 Python 3.13 → 3.12 → 3.11 순으로 찾는다(3.14 이상은 빌드 도구가 필요할 수 있음)
$pyver = $null
$ErrorActionPreference = "Continue"   # 없는 버전을 찾을 때 py 의 오류 출력으로 스크립트가 멈추지 않게
foreach ($v in "3.13", "3.12", "3.11") {
  try { & py "-$v" -c "import sys" *> $null; if ($LASTEXITCODE -eq 0) { $pyver = $v; break } } catch { }
}
$ErrorActionPreference = "Stop"
if (-not $pyver) {
  Write-Host ""
  Write-Host "Python 3.13(또는 3.12/3.11)이 설치되어 있지 않습니다." -ForegroundColor Yellow
  Write-Host "다음 중 하나로 설치한 뒤 이 스크립트를 다시 실행하세요:"
  Write-Host "  py install 3.13                      (Python 설치 관리자를 쓰는 경우)"
  Write-Host "  winget install Python.Python.3.13"
  exit 1
}
Write-Host "Python $pyver 사용"
if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
py -$pyver -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip | Out-Null
.\.venv\Scripts\pip install -e ".[dev]"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Host ""
  Write-Host "Node.js 가 설치되어 있지 않습니다(웹 화면 빌드에 필요)." -ForegroundColor Yellow
  Write-Host "  winget install OpenJS.NodeJS.LTS"
  Write-Host "설치 후 PowerShell 창을 새로 열고 이 스크립트를 다시 실행하세요."
  exit 1
}
Push-Location apps\web; npm ci; npm run build; Pop-Location
# 바탕화면 바로가기(AICC) 생성 — 이후 이 아이콘 하나만 실행
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "AICC.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = (Resolve-Path ".\.venv\Scripts\aicc.exe").Path
$s.WorkingDirectory = (Get-Location).Path
$s.Save()
Write-Host "설치 완료. 바탕화면의 AICC 아이콘을 실행하세요."
