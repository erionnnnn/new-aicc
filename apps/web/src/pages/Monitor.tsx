import { useState } from "react";
import { api, ApiError, CLIP_LABEL, koDate, Me, OUTCOME } from "../api";
import { ErrorBox, Notice, OutcomeBadge, Page, useAsync } from "../ui";

const LADDER: { key: string; label: string; plays: string[] }[] = [
  { key: "OPENING", label: "인사·통화 가능 확인", plays: ["opening", "ask_repeat"] },
  { key: "ASK_WORKING", label: "현재 근무 여부", plays: ["ask_working"] },
  { key: "ASK_END_DATE", label: "근무 종료일", plays: ["ask_end_date"] },
  { key: "ASK_AVAILABLE_DATE", label: "다음 근무 가능일", plays: ["ask_available_date"] },
  { key: "CONFIRM_DATE", label: "날짜 재확인", plays: ["confirm_date"] },
  { key: "CLOSING", label: "마무리", plays: ["closing", "refusal_ack", "callback", "wrong_person", "ai_failed"] },
];

function stageOf(events: any[]): { stage: string; ai: boolean } {
  let stage = "DIALING";
  let ai = false;
  for (const e of events) {
    if (e.type === "call.answered") stage = "OPENING";
    if (e.type === "play") {
      const s = LADDER.find((l) => l.plays.includes(e.payload.key));
      if (s) stage = s.key;
      ai = false;
    }
    if (e.type === "ai.start") ai = true;
    if (e.type === "ai.done" || e.type === "ai.failed") ai = false;
  }
  return { stage, ai };
}

function CurrentCall({ call }: { call: any }) {
  const events: any[] = call.events ?? [];
  const { stage, ai } = stageOf(events);
  const cand = [...events].reverse().find((e) => e.type === "schedule.candidate");
  const confirmed = [...events].reverse().find((e) => e.type === "schedule.confirmed");
  const said = events.filter((e) => e.type === "utterance.final" || e.type === "ai.done" || e.type === "play").slice(-12);
  const idx = LADDER.findIndex((l) => l.key === stage);
  return (
    <div className="callcard">
      <div className="who">
        <div className="who-name">{call.name}</div>
        <div className="who-meta num">{call.external_no} · {call.phone}</div>
        <div className="who-meta">
          {call.answered_at ? `응답 ${call.answered_at.slice(11)}` : "발신 중 — 응답 확인 전"}
          {call.is_mock && <span className="badge warn">MOCK</span>}
        </div>
      </div>
      <ol className="ladder" aria-label="통화 단계">
        {LADDER.map((l, i) => (
          <li key={l.key} className={i < idx ? "past" : i === idx ? "now" : ""} aria-current={i === idx ? "step" : undefined}>
            {l.label}
            {i === idx && ai && <span className="ai-tag">예외 질문 응대 중</span>}
          </li>
        ))}
      </ol>
      <div className="candidate" aria-live="polite">
        <div className="candidate-label">{confirmed ? "상대가 확인한 다음 근무 가능일" : cand ? "날짜 후보(재확인 전)" : "날짜 후보"}</div>
        <div className={`candidate-date ${confirmed ? "confirmed" : cand ? "pending" : "none"}`}>
          {confirmed?.payload?.date ?? cand?.payload?.date ?? "아직 없음"}
        </div>
        {cand && !confirmed && <div className="candidate-note">“{koDate(cand.payload.date)}부터 가능하신 게 맞으실까요?”에 명시적으로 동의해야 반영됩니다.</div>}
      </div>
      <div className="transcript">
        {said.map((e) => (
          <div key={e.id} className={`line ${e.type === "utterance.final" ? "them" : "us"}`}>
            <span className="who-tag">{e.type === "utterance.final" ? "상대" : e.type === "ai.done" ? "AI" : "ARS"}</span>
            <span>{e.type === "play" ? (e.payload.key === "confirm_date" ? `“${koDate(e.payload.params?.date ?? cand?.payload?.date ?? "")}부터 가능하신 게 맞으실까요?”` : CLIP_LABEL[e.payload.key] ?? e.payload.key) : e.payload.text}</span>
          </div>
        ))}
        {said.length === 0 && <div className="muted">아직 대화가 없습니다.</div>}
      </div>
    </div>
  );
}

export default function MonitorPage({ me, campaignId }: { me: Me; campaignId: string }) {
  const live = useAsync(() => api.get<any>(`/api/v1/campaigns/${campaignId}/live`), [campaignId], 1000);
  const [err, setErr] = useState<ApiError | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const d = live.data;

  async function act(kind: "pause" | "stop") {
    setErr(null);
    try {
      await api.post(`/api/v1/campaigns/${campaignId}/${kind}`, kind === "stop" ? { mode: "immediate", expected_version: d.version } : { expected_version: d.version });
      setConfirmStop(false);
      live.reload();
    } catch (e) { setErr(e as ApiError); }
  }

  if (!d) return <Page title="통화 진행"><ErrorBox error={live.error} /></Page>;
  const canControl = me.can.upload_dial && ["RUNNING", "PAUSED", "STOPPING"].includes(d.state);
  return (
    <Page title="통화 진행" actions={canControl && (
      <>
        <button className="btn" disabled={d.state !== "RUNNING"} onClick={() => act("pause")}>현재 통화 후 일시정지</button>
        {confirmStop ? (
          <span className="confirm-inline">
            <button className="btn danger" onClick={() => act("stop")}>지금 바로 중단</button>
            <button className="btn ghost" onClick={() => setConfirmStop(false)}>취소</button>
          </span>
        ) : <button className="btn danger" disabled={d.state === "STOPPING"} onClick={() => setConfirmStop(true)}>즉시중단</button>}
      </>
    )}>
      <div className="progress-line">
        <strong>{d.name}</strong>
        <span className="num">완료 {d.done} / 전체 {d.total}</span>
        <span>{({ RUNNING: "진행 중", PAUSED: "일시정지 — 재개하려면 통화 준비에서 다시 시작", STOPPING: "통화 종료 확인 중", STOPPED: "중단됨", COMPLETED: "모든 통화 완료", DRAFT: "시작 전" } as any)[d.state]}</span>
        {d.state === "PAUSED" && <a href={`#/prepare?campaign=${d.id}`}>통화 준비로</a>}
        {d.state === "COMPLETED" && <a href={`#/export?campaign=${d.id}`}>결과 내려받기</a>}
      </div>
      <progress max={Math.max(1, d.total)} value={d.done} aria-label="진행률" />
      {d.state === "STOPPING" && <Notice kind="warn">종료 명령을 보냈습니다. 케이블이 빠져 종료 명령을 보낼 수 없으면 A24에서 직접 통화를 끊으세요.</Notice>}
      {d.device && !d.device.connected && d.state === "RUNNING" && (
        <Notice kind="danger">단말 연결 신호가 없습니다. 다음 발신은 멈춰 있습니다. <a href="#/settings?tab=devices">단말 진단</a></Notice>
      )}
      <ErrorBox error={err} />
      {d.current ? <CurrentCall call={d.current} /> : <div className="panel muted">지금 진행 중인 통화가 없습니다.</div>}
      <div className="two-col">
        <div className="panel">
          <h2>대기</h2>
          {d.queue.length === 0 ? <p className="muted">대기 중인 대상이 없습니다.</p> : (
            <ul className="plain">{d.queue.map((q: any, i: number) => <li key={i}><span className="num">{q.external_no}</span> {q.name} <span className="muted num">{q.phone}</span></li>)}</ul>
          )}
        </div>
        <div className="panel">
          <h2>완료</h2>
          <table className="table compact">
            <thead><tr><th>이름</th><th>결과</th><th>가능일</th><th>종료(KST)</th></tr></thead>
            <tbody>
              {d.recent.map((r: any) => (
                <tr key={r.id}>
                  <td><a href={`#/review?call=${r.id}`}>{r.name}</a></td>
                  <td><OutcomeBadge outcome={r.outcome} /></td>
                  <td className="num">{r.confirmation_state === "CONFIRMED" ? r.available_date : <span className="unknown">{r.confirmation_state === "ANYTIME" ? "상시 가능" : "미확인"}</span>}</td>
                  <td className="num">{r.ended_at?.slice(11)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <p className="muted small">결과 분포: {Object.entries(d.outcomes).map(([k, v]) => `${OUTCOME[k] ?? "진행 중"} ${v}`).join(", ") || "-"}</p>
    </Page>
  );
}
