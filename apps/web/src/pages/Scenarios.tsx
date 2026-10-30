import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { Empty, ErrorBox, Field, Notice, Page, useAsync } from "../ui";

const KIND: Record<string, string> = { INFO: "답하고 일정 다시 물음", REVIEW: "담당자 확인 + 일정 물음", PREF: "선호 메모 + 일정 물음", END: "정중히 종료" };
const OUT: Record<string, string> = { CALLBACK_REQUESTED: "재연락", UNCONFIRMED: "미확정" };

function Proposal({ pr, me, onDone }: { pr: any; me: Me; onDone: () => void }) {
  const d = pr.draft || {};
  const [text, setText] = useState<string>(d.text || "");
  const [kind, setKind] = useState<string>(d.kind || "INFO");
  const [outcome, setOutcome] = useState<string>(d.outcome || "UNCONFIRMED");
  const [checks, setChecks] = useState<any>(pr.checks);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const isNew = pr.action === "NEW";
  const edits = isNew ? { text, kind, outcome: kind === "END" ? outcome : undefined } : undefined;
  const changed = isNew && (text !== d.text || kind !== d.kind || (kind === "END" && outcome !== d.outcome));

  async function act(path: string, body: any) {
    setErr(null); setBusy(true);
    try { return await api.post(`/api/v1/scenario-proposals/${pr.id}/${path}`, body); }
    catch (e) { setErr(e as ApiError); return null; } finally { setBusy(false); }
  }
  return (
    <div className="panel">
      <h3>
        {isNew ? <span className="badge warn">새 답</span> : <span className="badge muted">표현 추가</span>}{" "}
        {isNew ? d.code : `기존 답 ${pr.target_code}에 표현 추가`} <span className="muted small">· {pr.evidence?.count ?? 0}번 들어온 말 · {pr.created_at}</span>
      </h3>
      <p className="small"><b>실제 들어온 말</b></p>
      <ul className="detail">{(d.examples || []).map((t: string) => <li key={t}>“{t}”</li>)}</ul>
      {isNew ? (
        <>
          <Field label="녹음할 답 문구(고칠 수 있음)"><textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} maxLength={150} /></Field>
          <div className="grid3">
            <Field label="답한 뒤">
              <select value={kind} onChange={(e) => setKind(e.target.value)}>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
            </Field>
            {kind === "END" && (
              <Field label="통화 결과"><select value={outcome} onChange={(e) => setOutcome(e.target.value)}>{Object.entries(OUT).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            )}
            {d.review && <Field label="검수 표시"><input value={d.review} readOnly /></Field>}
          </div>
          {d.why && <p className="muted small">AI 이유: {d.why}</p>}
        </>
      ) : null}
      <div className={`checks ${checks?.ok ? "" : "bad"}`}>
        <p className="small"><b>자동 검사</b> {checks?.ok ? <span className="badge ok">통과</span> : <span className="badge danger">불합격</span>}
          {checks?.corpus && <> · 대화 시험 {checks.corpus.after}/{checks.corpus.total} (변경 전 {checks.corpus.before})</>}</p>
        {(checks?.errors || []).map((x: string) => <p key={x} className="small danger-text">· {x}</p>)}
        {(checks?.warnings || []).map((x: string) => <p key={x} className="small muted">· {x}</p>)}
      </div>
      <ErrorBox error={err} />
      {msg && <Notice kind="ok">{msg}</Notice>}
      {me.can.settings && pr.status === "PENDING" && (
        <div className="actions">
          {changed && <button className="btn" disabled={busy} onClick={async () => { const r = await act("recheck", { edits }); if (r) setChecks(r.checks); }}>고친 문구로 다시 검사</button>}
          <button className="btn primary" disabled={busy || (!changed && !checks?.ok)} onClick={async () => {
            const r = await act("approve", { edits: changed ? edits : undefined });
            if (r) {
              const v = r.voice || {};
              setMsg(isNew ? `승인했습니다(${r.code}). 음원: 여성 ${v.female ?? "-"}, 남성 ${v.male ?? "-"}. 설정 › 음원팩에서 들어 보고 승인하면 새로 만드는 캠페인부터 씁니다.`
                : "승인했습니다. 다음 통화부터 이 표현도 같은 답으로 연결됩니다.");
              onDone();
            }
          }}>승인</button>
          <button className="btn ghost" disabled={busy} onClick={async () => { const r = await act("reject", {}); if (r) onDone(); }}>거절</button>
        </div>
      )}
    </div>
  );
}

function AutoApply({ me }: { me: Me }) {
  const st = useAsync(() => api.get<any>("/api/v1/settings"));
  const [err, setErr] = useState<ApiError | null>(null);
  if (!me.can.settings || !st.data) return null;
  const on = !!st.data.policies?.auto_apply_examples;
  return (
    <>
      <label className="inline small"><input type="checkbox" checked={on} onChange={async (e) => {
        setErr(null);
        try { await api.put("/api/v1/settings", { policies: { auto_apply_examples: e.target.checked } }); st.reload(); } catch (e2) { setErr(e2 as ApiError); }
      }} /> 검사를 통과한 ‘표현 추가’는 자동 반영(새 답은 항상 직접 승인)</label>
      <ErrorBox error={err} />
    </>
  );
}

function Proposals({ me }: { me: Me }) {
  const [status, setStatus] = useState("PENDING");
  const list = useAsync(() => api.get<any[]>(`/api/v1/scenario-proposals?status=${status}`), [status]);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  return (
    <>
      <div className="panel">
        <p className="lead">매일 밤 9시에 최근 통화에서 자동 응답이 알아듣지 못한 말을 모아, AI가 새 답이나 기존 답의 표현 추가를 제안합니다.
          대화 시험·문구 규칙 검사를 통과한 제안만 승인할 수 있고, 승인한 것만 실제 통화에 쓰입니다.</p>
        <AutoApply me={me} />
        <div className="toolbar">
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="상태">
            <option value="PENDING">승인 대기</option><option value="APPROVED">승인함</option><option value="REJECTED">거절함</option><option value="ALL">전체</option>
          </select>
          <span className="spacer" />
          {me.can.settings && <button className="btn" disabled={busy} onClick={async () => {
            setErr(null); setMsg(""); setBusy(true);
            try { const r = await api.post("/api/v1/scenario-proposals/generate", { days: 7 }); setMsg(`제안 ${r.made}건을 만들었습니다.`); list.reload(); }
            catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
          }}>{busy ? "분석 중… (1분 안팎)" : "지금 최근 7일 통화 분석"}</button>}
        </div>
        <ErrorBox error={err} />
        {msg && <Notice kind="ok">{msg}</Notice>}
      </div>
      {(list.data ?? []).length === 0 ? <Empty>{status === "PENDING" ? "승인 대기 중인 제안이 없습니다." : "제안이 없습니다."}</Empty>
        : (list.data ?? []).map((pr) => <Proposal key={pr.id} pr={pr} me={me} onDone={list.reload} />)}
    </>
  );
}

function Catalog({ me }: { me: Me }) {
  const list = useAsync(() => api.get<any[]>("/api/v1/scenarios"));
  const [q, setQ] = useState("");
  const [src, setSrc] = useState("all");
  const [err, setErr] = useState<ApiError | null>(null);
  const rows = (list.data ?? []).filter((r) => (src === "all" || r.source === src) && (!q || r.text.includes(q) || r.code.includes(q.toUpperCase())));
  return (
    <div className="panel">
      <div className="toolbar">
        <input type="search" placeholder="문구·코드 검색" value={q} onChange={(e) => setQ(e.target.value)} aria-label="검색" />
        <select value={src} onChange={(e) => setSrc(e.target.value)} aria-label="출처">
          <option value="all">전체 {(list.data ?? []).length}개</option><option value="learned">통화로 배운 답</option><option value="faq">예상 질문</option><option value="base">기존 녹음 상황</option>
        </select>
      </div>
      <ErrorBox error={err} />
      <div className="table-wrap">
        <table className="table compact">
          <thead><tr><th>사용</th><th>코드</th><th>답 문구</th><th>답한 뒤</th><th>배운 표현</th><th>녹음</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code} className={r.enabled ? "" : "dim"}>
                <td><input type="checkbox" checked={r.enabled} disabled={!me.can.settings} aria-label={`${r.code} 사용`} onChange={async (e) => {
                  setErr(null);
                  try { await api.put(`/api/v1/scenarios/${r.code}`, { enabled: e.target.checked }); list.reload(); } catch (e2) { setErr(e2 as ApiError); }
                }} /></td>
                <td className="num small">{r.code}{r.source === "learned" && <span className="badge ok">배움</span>}</td>
                <td>{r.text}</td>
                <td className="small">{KIND[r.kind]}{r.outcome ? ` (${OUT[r.outcome] ?? r.outcome})` : ""}</td>
                <td className="small">{r.examples.length ? r.examples.slice(0, 3).join(" / ") : <span className="muted">-</span>}</td>
                <td>{r.has_recording ? <span className="badge ok">있음</span> : <span className="badge warn">없음</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">끈 답은 다음 통화부터 쓰지 않습니다(그 말은 AI가 받거나 다시 묻습니다). 녹음이 없는 답도 쓰이지 않습니다.</p>
    </div>
  );
}

function Effects() {
  const [days, setDays] = useState(30);
  const st = useAsync(() => api.get<any>(`/api/v1/scenarios/stats?days=${days}`), [days]);
  const d = st.data;
  return (
    <div className="panel">
      <div className="toolbar">
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="기간">
          <option value={7}>최근 7일</option><option value={30}>최근 30일</option><option value={90}>최근 90일</option>
        </select>
      </div>
      <ErrorBox error={st.error} />
      {d && (
        <>
          <dl className="stats">
            <div><dt>통화</dt><dd>{d.calls}</dd></div>
            <div><dt>AI로 넘어간 말</dt><dd>{d.ai.total}</dd></div>
            <div><dt>녹음 답으로 연결</dt><dd>{d.ai.routed}</dd></div>
            <div><dt>AI가 직접 답함</dt><dd>{d.ai.generated}</dd></div>
            <div><dt>그 직후 상대 종료</dt><dd>{d.ai.hangup_after}</dd></div>
          </dl>
          {d.answers.length === 0 ? <p className="muted">이 기간에 재생된 상황 답이 없습니다.</p> : (
            <div className="table-wrap">
              <table className="table compact">
                <thead><tr><th>답</th><th>재생</th><th>직후 상대 종료</th><th>비율</th></tr></thead>
                <tbody>{d.answers.map((r: any) => (
                  <tr key={r.key} className={r.plays >= 5 && r.hangup_rate >= 40 ? "warn-row" : ""}>
                    <td className="num small">{r.code}</td><td className="num">{r.plays}</td><td className="num">{r.hangup_after}</td>
                    <td className="num">{r.hangup_rate == null ? "-" : `${r.hangup_rate}%`}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          )}
          <p className="muted small">‘직후 상대 종료’는 그 답이 나간 뒤 상대가 다른 말 없이 끊은 경우입니다. 5번 넘게 쓰였는데 40% 이상이면 문구를 고쳐 보세요.</p>
        </>
      )}
    </div>
  );
}

export default function ScenariosPage({ me, tab }: { me: Me; tab: string }) {
  return (
    <Page title="시나리오">
      <div className="segmented" role="tablist">
        {[["proposals", "AI 제안"], ["catalog", "답 목록"], ["effects", "답별 효과"]].map(([k, l]) => (
          <a key={k} role="tab" aria-selected={tab === k} href={`#/scenarios?tab=${k}`} className="seg">{l}</a>
        ))}
      </div>
      {tab === "catalog" ? <Catalog me={me} /> : tab === "effects" ? <Effects /> : <Proposals me={me} />}
    </Page>
  );
}
