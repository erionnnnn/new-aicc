# MeloTTS(이 PC 의 CPU 로 한국어 안내 음성 생성) 설치 — AICC 와 별도 환경(.venv-melo)
# MeloTTS 는 Python 3.9~3.11 기준 오래된 의존성(transformers 4.27 등)이라 AICC 환경(3.11+)과 섞지 않는다.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$pyver = $null
$ErrorActionPreference = "Continue"
foreach ($v in "3.11", "3.10") {
  try { & py "-$v" -c "import sys" *> $null; if ($LASTEXITCODE -eq 0) { $pyver = $v; break } } catch { }
}
$ErrorActionPreference = "Stop"
if (-not $pyver) {
  Write-Host ""
  Write-Host "MeloTTS 에는 Python 3.11(또는 3.10)이 필요합니다(AICC 의 3.13 과 따로 설치됩니다)." -ForegroundColor Yellow
  Write-Host "다음 중 하나로 설치한 뒤 setup_melo.bat 을 다시 실행하세요:"
  Write-Host "  py install 3.11"
  Write-Host "  winget install Python.Python.3.11"
  exit 1
}
Write-Host "MeloTTS 용 Python $pyver 사용"
$melo = Join-Path (Get-Location) ".venv-melo"
$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($melo, [StringComparison]::OrdinalIgnoreCase) }
if ($running) { Write-Host "MeloTTS 가 실행 중입니다. AICC 를 모두 닫고 다시 실행하세요." -ForegroundColor Yellow; exit 1 }
if (-not (Test-Path .\.venv-melo\Scripts\python.exe)) { py -$pyver -m venv .venv-melo }
$p = ".\.venv-melo\Scripts\python.exe"
& $p -m pip install --upgrade pip wheel "setuptools<70" | Out-Null
Write-Host "MeloTTS 설치 중(처음에는 PyTorch 등 1GB 안팎을 내려받아 10분 이상 걸릴 수 있습니다)..."
# 검증한 버전으로 고정(2026-10-28 확인한 MeloTTS main 커밋)
& $p -m pip install "git+https://github.com/myshell-ai/MeloTTS.git@209145371cff8fc3bd60d7be902ea69cbdb7965a"
if ($LASTEXITCODE -ne 0) { Write-Host "MeloTTS 설치 실패 — 위 오류를 캡처해 보내 주세요." -ForegroundColor Red; exit 1 }
# 한국어 발음 변환: Windows 설치 파일이 있는 python-mecab-ko 사용(eunjeon 은 C++ 빌드 도구 필요라 쓰지 않음)
& $p -m pip install python-mecab-ko soundfile
if ($LASTEXITCODE -ne 0) { Write-Host "한국어 형태소 분석기(python-mecab-ko) 설치 실패" -ForegroundColor Red; exit 1 }
& $p -c "import nltk; [nltk.download(x, quiet=True) for x in ('cmudict', 'averaged_perceptron_tagger', 'averaged_perceptron_tagger_eng')]"
Write-Host "한국어 모델 내려받고 시험 문장 만드는 중(처음 1회, 수 분)..."
& $p scripts\melo_server.py --warmup
if ($LASTEXITCODE -ne 0) { Write-Host "시험 문장 생성 실패 — 위 오류를 캡처해 보내 주세요." -ForegroundColor Red; exit 1 }
Write-Host ""
Write-Host "MeloTTS 설치 완료. AICC 를 다시 켜면 음성 서버가 자동으로 함께 켜집니다." -ForegroundColor Green
Write-Host "설정 > 음원팩(여성) > 'MeloTTS로 전체 만들기'를 누르세요."
