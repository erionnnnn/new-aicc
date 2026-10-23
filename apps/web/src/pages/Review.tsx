import { useState } from "react";
import { api, ApiError, CLIP_LABEL, Me } from "../api";
import { go } from "../App";
import { DateCell, Empty, ErrorBox, Field, Notice, OutcomeBadge, Page, useAsync } from "../ui";

const REASON: Record<string, string> = {
  AI_FAILED: "AI 응답 실패", AI_LIMIT: "예외 질문 횟수 초과", NO_RESPONSE: "무응답", WRONG_PERSON: "잘못된 상대",
  DATE_RANGE_ONLY: "날짜 범위만 응답", DATE_UNKNOWN: "날짜 모름", MAX_CALL_DURATION: "최대 통화 시간 초과",
  ANSWER_DETECTION_UNKNOWN: "응답 감지 불가", HANGUP_FAILED_MANUAL: "종료 명령 실패(단말에서 직접 확인)",
  TOO_MANY_CORRECTIONS: "날짜 정정 반복", SERVER_ERROR: "서버 오류",
};
const reasonText = (r: string) => REASON[r] ?? (r.startsWith("UNCLEAR_") ? "응답 불명확" : r.startsWith("CONFIRM_REJECTED") ? "서버가 확정 거부" : r);

function Player({ rec, canPlay, callEnded }: { rec: any; canPlay: boolean; callEnded: boolean }) {
  const [url, setUrl] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  if (!rec) return <span className="muted">녹음 없음</span>;
  if (rec.state !== "READY") return <span className="badge warn">{({ FAILED: "녹음 저장 실패", EXPIRED: "보관기간 만료", PENDING: "업로드 대기" } as any)[rec.state] ?? rec.state}{rec.error ? `: ${rec.error}` : ""}</span>;
  if (!canPlay) return <span className="muted">재생 권한 없음</span>;
  if (!callEnded) return <span className="muted">통화 종료 후 재생 가능</span>;
  return (
    <div>
      {url ? <audio controls src={url} preload="none" /> : (
        <button className="btn ghost small" onClick={async () => {
          try { setUrl((await api.post(`/api/v1/recordings/${rec.id}/playback`)).url); } catch (e) { setErr(e as ApiError); }
        }}>녹음 듣기(헤드셋 권장)</button>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

function Detail({ me, callId, onDone }: { me: Me; callId: string; onDone: () => void }) {
  const call = useAsync(() => api.get<any>(`/api/v1/calls/${callId}`), [callId]);
  const [date, setDate] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [msg, setMsg] = useState("");
  const c = call.data;
  if (!c) return <ErrorBox error={call.error} />;
  const obs = c.observation;
  const cand = [...c.events].reverse().find((e: any) => e.type === "schedule.candidate");
  const evidence = obs?.evidence_event_id ? c.events.find((e: any) => e.id === obs.evidence_event_id) : null;
  const canEdit = me.can.edit_dates;

  async function save() {
    setErr(null);
    try {
      await api.patch(`/api/v1/caregivers/${c.caregiver.id}/schedule`, { date: date || null, expected_version: c.caregiver.version, reason, call_id: c.id });
      await api.post(`/api/v1/calls/${c.id}/review`, { action: "done", note: note || undefined });
      setMsg("수정하고 검수를 마쳤습니다.");
      call.reload(); onDone();
    } catch (e) { setErr(e as ApiError); call.reload(); }
  }
  async function mark(action: "done" | "hold") {
    setErr(null);
    try {
      await api.post(`/api/v1/calls/${c.id}/review`, { action, note: note || undefined });
      setMsg(action === "done" ? "검수를 마쳤습니다." : "보류했습니다.");
      call.reload(); onDone();
    } catch (e) { setErr(e as ApiError); }
  }

  return (
    <div className="panel detail">
      <h2>{c.name} <span className="num muted">{c.external_no}</span> <OutcomeBadge outcome={c.outcome} /></h2>
      <div className="compare">
        <div>
          <h3>명부의 현재 값</h3>
          <p>근무확정일 <DateCell value={c.caregiver?.confirmed_available_date} /></p>
          <p>다음근무가능일 <DateCell value={c.caregiver?.next_available_date} /></p>
          <h3>이번 통화</h3>
          <p>근무 상태: {obs?.work_status === "WORKING" ? "근무중" : obs?.work_status === "NOT_WORKING" ? "근무 안 함" : "미확인"}</p>
          <p>종료일(진술): <DateCell value={obs?.end_date} /></p>
          <p>가능일 후보: <DateCell value={obs?.available_date || cand?.payload?.date} confirmed={obs?.confirmation_state === "CONFIRMED"} />
            {" "}{obs?.confirmation_state === "CONFIRMED" ? <span className="badge ok">상대 확인</span> : <span className="badge warn">자동 반영 안 됨</span>}</p>
          {obs?.range?.[0] && <p>응답 범위: {obs.range[0]} ~ {obs.range[1]} (날짜 아님)</p>}
          {(c.review_reasons ?? []).length > 0 && <p>검수 사유: {c.review_reasons.map(reasonText).join(", ")}</p>}
        </div>
        <div>
          <h3>근거 발화</h3>
          {evidence ? <blockquote>“{evidence.payload.text}” <span className="muted num">{evidence.at}</span></blockquote> : <p className="muted">확정 근거 발화 없음</p>}
          <h3>대화 기록</h3>
          <div className="transcript tall">
            {c.events.filter((e: any) => ["utterance.final", "play", "ai.done", "schedule.candidate", "schedule.confirmed", "contact.blocked", "silence.timeout"].includes(e.type)).map((e: any) => (
              <div key={e.id} className={`line ${e.type === "utterance.final" ? "them" : "us"}`}>
                <span className="who-tag num">{e.at?.slice(11)}</span>
                <span>{e.type === "utterance.final" ? `상대: ${e.payload.text}` : e.type === "play" ? `ARS: ${CLIP_LABEL[e.payload.key] ?? e.payload.key}` : e.type === "ai.done" ? `AI: ${e.payload.text ?? ""}` : e.type === "schedule.candidate" ? `날짜 후보 ${e.payload.date}` : e.type === "schedule.confirmed" ? `반영 ${e.payload.date}` : e.type === "contact.blocked" ? "연락거부 저장" : "침묵 재질문"}</span>
              </div>
            ))}
          </div>
          <h3>녹음</h3>
          <Player rec={c.recording} canPlay={me.can.play_recording} callEnded={!!c.ended_at} />
        </div>
      </div>
      {canEdit ? (
        <div className="edit-box">
          <h3>검수 처리</h3>
          <div className="grid3">
            <Field label="반영할 날짜(비우면 빈 값)"><input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
            <Field label="수정 사유(필수)"><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="예: 녹음 재청취로 27일 확인" /></Field>
            <Field label="검수 메모"><input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
          </div>
          <ErrorBox error={err} />
          {msg && <Notice kind="ok">{msg}</Notice>}
          <div className="actions">
            <button className="btn primary" disabled={reason.trim().length < 2} onClick={save}>날짜 수정하고 검수 완료</button>
            <button className="btn" onClick={() => mark("done")}>수정 없이 검수 완료</button>
            <button className="btn ghost" onClick={() => mark("hold")}>보류</button>
          </div>
          <p className="muted small">
            <button className="btn ghost small danger-text" onClick={async () => {
              const r = window.prompt("연락거부로 등록할까요? 사유를 적어 주세요(예: 문자로 거부 의사 전달)");
              if (!r || r.trim().length < 2) return;
              try { await api.post("/api/v1/contact-blocks", { caregiver_id: c.caregiver.id, reason: r.trim(), call_id: c.id }); setMsg("연락거부로 등록했습니다. 대기 중인 발신에서도 제외됩니다."); } catch (e) { setErr(e as ApiError); }
            }}>연락거부 등록</button>
          </p>
          <p className="muted small">이전 값은 변경이력에 남습니다. 다른 사람이 먼저 바꿨다면 최신 값을 보여주고 저장하지 않습니다.</p>
        </div>
      ) : <p className="muted">날짜 수정 권한이 없습니다.</p>}
    </div>
  );
}

export default function ReviewPage({ me, callId }: { me: Me; callId: string | null }) {
  const [state, setState] = useState("PENDING");
  const list = useAsync(() => api.get<any[]>(`/api/v1/reviews?state=${state}`), [state]);
  const items = list.data ?? [];
  return (
    <Page title="결과 검수" actions={
      <select value={state} onChange={(e) => setState(e.target.value)} aria-label="검수 상태">
        <option value="PENDING">검수 필요</option><option value="DONE">검수 완료</option><option value="ALL">전체 통화</option>
      </select>
    }>
      <div className="split">
        <div className="list-col">
          {items.length === 0 ? <Empty>{state === "PENDING" ? "검수할 통화가 없습니다." : "통화가 없습니다."}</Empty> : (
            <ul className="selectable">
              {items.map((r) => (
                <li key={r.id}>
                  <button className={callId === r.id ? "active" : ""} onClick={() => go("/review", { call: r.id })}>
                    <span>{r.name} <span className="num muted">{r.external_no}</span></span>
                    <OutcomeBadge outcome={r.outcome} />
                    <span className="num muted small">{r.started_at}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="detail-col">
          {callId ? <Detail me={me} callId={callId} onDone={list.reload} /> : <Empty>왼쪽에서 통화를 고르세요.</Empty>}
        </div>
      </div>
    </Page>
  );
}
