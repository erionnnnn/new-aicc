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
# AICC 가 켜져 있으면 설치 파일이 잠겨 실패한다 → 먼저 닫도록 안내
$venvDir = (Join-Path (Get-Location) ".venv")
$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($venvDir, [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
  Write-Host ""
  Write-Host "AICC(또는 그 Python)가 실행 중입니다: $(($running | Select-Object -ExpandProperty ProcessName -Unique) -join ', ')" -ForegroundColor Yellow
  Write-Host "AICC 검은 창을 모두 닫은 뒤 setup.bat 을 다시 실행하세요."
  exit 1
}
# 기존 가상환경은 정상이면 그대로 쓴다(매번 지우면 느리고, 파일이 잠겨 있으면 반쯤 지워진 채 망가짐)
$venvOk = $false
if (Test-Path .\.venv\Scripts\python.exe) {
  $ErrorActionPreference = "Continue"
  & .\.venv\Scripts\python -c "import sys, aicc_api, aicc_agent; assert '%d.%d' % sys.version_info[:2] == '$pyver'" *> $null
  if ($LASTEXITCODE -eq 0) {
    & .\.venv\Scripts\python -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('faster_whisper') is None else __import__('faster_whisper') and 0)" *> $null
    if ($LASTEXITCODE -eq 0) { $venvOk = $true }
  }
  $ErrorActionPreference = "Stop"
}
if (-not $venvOk) {
  if (Test-Path .venv) {
    Write-Host "가상환경이 손상되어 새로 만듭니다"
    try { Remove-Item -Recurse -Force .venv -ErrorAction Stop }
    catch {
      Write-Host "가상환경(.venv)을 지우지 못했습니다(파일 사용 중). AICC 창을 모두 닫고, 그래도 안 되면 PC 를 재시작한 뒤 다시 실행하세요." -ForegroundColor Yellow
      exit 1
    }
  }
  py -$pyver -m venv .venv
} else {
  Write-Host "기존 가상환경 사용(업데이트만 설치)"
}
.\.venv\Scripts\python -m pip install --upgrade pip | Out-Null
.\.venv\Scripts\pip install -e ".[dev]"
# 안내 음성 자동 생성용(선택). 실패해도 설치는 계속한다.
& .\.venv\Scripts\pip install -e ".[tts]"
if ($LASTEXITCODE -ne 0) { Write-Host "안내 음성 자동 생성 모듈 설치 실패 — 녹음 파일 업로드나 Windows 음성은 사용 가능" -ForegroundColor Yellow }
& .\.venv\Scripts\pip install -e ".[voice]"
if ($LASTEXITCODE -ne 0) { Write-Host "음성 인식(faster-whisper) 모듈 설치 실패 — 실통화 시험 전에 다시 실행하세요" -ForegroundColor Yellow }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Host ""
  Write-Host "Node.js 가 설치되어 있지 않습니다(웹 화면 빌드에 필요)." -ForegroundColor Yellow
  Write-Host "  winget install OpenJS.NodeJS.LTS"
  Write-Host "설치 후 PowerShell 창을 새로 열고 이 스크립트를 다시 실행하세요."
  exit 1
}
Push-Location apps\web; npm ci; npm run build; Pop-Location
# scrcpy 5.0.1(Windows 64bit) 내려받기 — 상대 음성(RX) USB 수신용. adb.exe 도 함께 들어 있다.
$scVer = "5.0.1"
$scSha = "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5"   # 공식 SHA256SUMS.txt 값
$scDir = Join-Path (Get-Location) "tools\scrcpy"
if (-not (Test-Path (Join-Path $scDir "scrcpy.exe"))) {
  Write-Host "scrcpy $scVer 내려받는 중..."
  New-Item -ItemType Directory -Force -Path "tools" | Out-Null
  $zip = Join-Path "tools" "scrcpy-win64-v$scVer.zip"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/Genymobile/scrcpy/releases/download/v$scVer/scrcpy-win64-v$scVer.zip" -OutFile $zip
  $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $scSha) { Remove-Item $zip; Write-Host "scrcpy 파일 검증 실패(해시 불일치). 설치를 중단합니다." -ForegroundColor Red; exit 1 }
  Expand-Archive -Force $zip -DestinationPath "tools"
  if (Test-Path $scDir) { Remove-Item -Recurse -Force $scDir }
  Rename-Item (Join-Path "tools" "scrcpy-win64-v$scVer") "scrcpy"
  Remove-Item $zip
}
Write-Host "scrcpy: $scDir"

# 바탕화면 바로가기(AICC) 생성 — 이후 이 아이콘 하나만 실행
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "AICC.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = (Resolve-Path ".\.venv\Scripts\aicc.exe").Path
$s.WorkingDirectory = (Get-Location).Path
$s.Save()
Write-Host "설치 완료. 바탕화면의 AICC 아이콘을 실행하세요."
