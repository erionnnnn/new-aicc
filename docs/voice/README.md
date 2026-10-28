# 안내 음성 녹음 대본

- `aicc_voice_script_female.json` / `aicc_voice_script_male.json` — 외부 TTS 도구(VoiceCraft 형식: speaker·gender·age·pitch·style·text·filename)에 넣는 대본. 목소리당 855개.
  - 기본 안내·상황 응답 62, 같은 상황 다른 표현 18, 날짜 조각 43(1월~12월, 1일~31일),
    날짜 재확인 통문장 366('10월 28일부터 근무 가능하신 게 맞으실까요?'), 종료일 확인 통문장 366.
  - 모두 고정 문장(통화 중 날짜 추정·생성 없음). 날짜 통문장이 있으면 조각 이어 붙이기 대신 쓴다.
- 만든 mp3 는 파일명을 바꾸지 말고 zip 으로 묶어 AICC **설정 > 안내 음성 > 녹음 zip 올리기**로 올린다.
  녹음 파일 자체는 저장소에 올리지 않는다(용량·음성 데이터).
- 문구를 고치면 `python scripts/make_voice_script.py` 로 다시 만든다(문구는 `packages/core/aicc_core/voicepack.py`, `scenarios.py`).
