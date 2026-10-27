// API 클라이언트: 같은 출처 쿠키 세션 + CSRF 완화 헤더(X-AICC)
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: any = {}) {
    super(message);
  }
}

function idemKey(): string {
  return (crypto as any).randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
}

async function req<T = any>(method: string, url: string, body?: any, opts: { idem?: boolean; form?: FormData } = {}): Promise<T> {
  const headers: Record<string, string> = { "X-AICC": "1" };
  let payload: BodyInit | undefined;
  if (opts.form) payload = opts.form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  if (opts.idem) headers["Idempotency-Key"] = idemKey();
  const r = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  if (!r.ok) {
    let err: any = { code: "HTTP_ERROR", message: `요청이 실패했습니다(${r.status})` };
    try {
      err = (await r.json()).error ?? err;
    } catch {
      /* 본문 없음 */
    }
    throw new ApiError(r.status, err.code, err.message, err.details);
  }
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("application/json") ? r.json() : (r.blob() as any)) as T;
}

export const api = {
  get: <T = any>(u: string) => req<T>("GET", u),
  post: <T = any>(u: string, b?: any, idem = false) => req<T>("POST", u, b ?? {}, { idem }),
  put: <T = any>(u: string, b?: any) => req<T>("PUT", u, b ?? {}),
  patch: <T = any>(u: string, b?: any) => req<T>("PATCH", u, b ?? {}),
  upload: <T = any>(u: string, file: File) => {
    const f = new FormData();
    f.append("file", file);
    return req<T>("POST", u, undefined, { form: f });
  },
};

export const OUTCOME: Record<string, string> = {
  CONFIRMED: "확인완료", ANYTIME: "상시 가능", UNCONFIRMED: "미확인", CALLBACK_REQUESTED: "재연락 요청",
  REFUSED: "연락거부", WRONG_PERSON: "잘못된 상대", VOICEMAIL: "음성사서함", NO_ANSWER: "부재중",
  LINE_BUSY: "통화중", DIAL_FAILED: "발신 실패", NO_RESPONSE: "무응답", CONSULT_REQUESTED: "상담 요청",
  REMOTE_HANGUP: "상대 종료", DEVICE_ERROR: "장치 오류", STOPPED: "운영자 중단",
};

export const ERROR_TEXT: Record<string, string> = {
  EXTERNAL_NO_EMPTY: "간병인 번호 없음", NAME_EMPTY: "이름 없음", PHONE_EMPTY: "연락처 없음",
  PHONE_INVALID: "연락처 형식 오류", PHONE_LEADING_ZERO_LOST: "앞자리 0 손실(숫자 셀)",
  PHONE_NOT_PERSONAL: "개인 번호 아님", DATE_INVALID: "날짜 형식 오류",
  DUP_EXTERNAL_NO_CONFLICT: "같은 번호·다른 연락처", DUP_ROW: "중복 행", SHARED_PHONE: "연락처 공유",
  BLOCKED: "연락거부", DB_DATE_DIFFERS: "통화 확인값 유지",
  NOT_TEST_NUMBER: "시험 번호 아님", ROW_ERROR: "행 오류", DUPLICATE: "중복", SHARED_PHONE_REVIEW: "연락처 공유 검수",
};

export type Me = {
  id: string; username: string; role: string; perms: Record<string, boolean>;
  can: Record<string, boolean>;
  organization: { id: string; name: string; environment: string; company_name: string };
};

export const CLIP_LABEL: Record<string, string> = {
  opening: "인사·통화 가능 확인", ask_working: "근무 여부 질문", ask_end_date: "종료일 질문",
  ask_available_date: "가능일 질문", confirm_date: "날짜 재확인", ask_repeat: "다시 말씀 요청",
  explain_role: "역할 안내", callback: "재연락 안내", refusal_ack: "연락거부 확인", closing: "마무리 인사",
  ai_wait: "잠시만요", ai_failed: "AI 실패 안내", wrong_person: "잘못된 연결 사과", ack: "맞장구(네,)",
  sc_ask_pay: "급여 질문 답변", sc_ask_hospital: "병원 질문 답변", sc_ask_patient: "환자 질문 답변", sc_ask_hours: "근무시간 질문 답변",
  sc_ask_job: "일감 질문 답변", sc_why_call: "전화 이유 답변", sc_who_are_you: "누구세요 답변", sc_how_got_number: "번호 출처 답변",
  sc_no_job_why_ask: "일감 없는데 왜 답변", sc_job_when_available: "일감 있을 때 연락 답변", sc_ai_question: "자동 안내 여부 답변",
  sc_emotional_hard: "공감", sc_didnt_hear: "다시 설명", sc_silence_check: "침묵 확인", sc_silence_end: "침묵 종료",
  sc_no_severe: "환자조건 선호", sc_no_night: "야간 불가", sc_weekday_only: "평일만", sc_weekend_only: "주말만",
  sc_preferred_hospital: "선호 병원", sc_high_pay_only: "급여 선호", sc_text_request: "문자 요청", sc_rest: "당분간 휴식",
  sc_unknown_schedule: "일정 미정", sc_driving: "운전 중", sc_available_now: "즉시 가능(미사용)",
};

export function koDate(iso: string): string {
  const [, m, d] = iso.split("-").map(Number);
  return `${m}월 ${d}일`;
}
