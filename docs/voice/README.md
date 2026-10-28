# 안내 음성 녹음 대본

- `aicc_voice_script_female.json` / `aicc_voice_script_male.json` — 외부 TTS 도구(VoiceCraft 형식: speaker·gender·age·pitch·style·text·filename)에 넣는 대본. 목소리당 855개.
  - 기본 안내·상황 응답 62, 같은 상황 다른 표현 18, 날짜 조각 43(1월~12월, 1일~31일),
    날짜 재확인 통문장 366('10월 28일부터 근무 가능하신 게 맞으실까요?'), 종료일 확인 통문장 366.
  - 모두 고정 문장(통화 중 날짜 추정·생성 없음). 날짜 통문장이 있으면 조각 이어 붙이기 대신 쓴다.
- 만든 mp3 는 파일명을 바꾸지 말고 zip 으로 묶어 AICC **설정 > 안내 음성 > 녹음 zip 올리기**로 올린다.
  녹음 파일 자체는 저장소에 올리지 않는다(용량·음성 데이터).
- 문구를 고치면 `python scripts/make_voice_script.py` 로 다시 만든다(문구는 `packages/core/aicc_core/voicepack.py`, `scenarios.py`).

## MeloTTS(이 PC 의 CPU, 무료·MIT) — 2026-10-28 추가

1. `setup_melo.bat` 더블클릭(처음 1회). Python 3.11(또는 3.10)이 없으면 `py install 3.11` 후 다시 실행.
   - AICC 와 별도 환경 `.venv-melo` 에 설치(MeloTTS 의존성이 오래돼 AICC 환경과 충돌).
   - 한국어 발음 변환은 Windows 설치 파일이 있는 `python-mecab-ko` 사용(eunjeon 은 C++ 빌드 도구가 필요해 쓰지 않음).
   - 일본어 사전은 큰 unidic 대신 함께 설치되는 unidic-lite 사용(한국어 음성과 무관).
   - 마지막에 모델을 내려받고 시험 문장을 만들어 설치를 확인.
2. AICC 를 다시 켜면 MeloTTS 음성 서버(127.0.0.1:3910)가 함께 켜진다(기록: `.aicc/melo.log`).
3. 설정 › 음원팩(여성) › **MeloTTS로 전체 만들기**.
   - 원본: 24kHz 모노 PCM 16비트(`packs/.../female/master/*-24k.wav`)
   - 통화 송출: 16kHz(휴대폰 송출 경로가 16kHz)
   - 이 엔진으로 만들면 통화 중 AI 답도 MeloTTS 로 읽는다(목소리 통일). 답 문장은 Gemini 가 만든다.
4. MeloTTS 한국어 목소리는 1개(여성) → 남성 음원팩에는 쓰지 않는다.
5. 속도 조절: `.aicc/agent.env` 에 `AICC_MELO_SPEED=1.1`(0.6~1.6). 끄기: `AICC_MELO=0`.
