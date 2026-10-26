import { FormEvent, useState } from "react";
import { api, ApiError, Me } from "../api";
import { go } from "../App";
import { ErrorBox, Field, Notice, Page, useAsync } from "../ui";

const TABS: [string, string][] = [["policy", "운영 정책"], ["voice", "안내 음성"], ["devices", "단말"], ["users", "사용자"], ["audit", "감사 기록"]];

function Policy({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const s = useAsync(() => api.get<any>("/api/v1/settings"));
  const [err, setErr] = useState<ApiError | null>(null);
  const [ok, setOk] = useState(false);
  if (!s.data) return <ErrorBox error={s.error} />;
  const p = s.data.policies;
  const admin = me.can.settings;

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setErr(null); setOk(false);
    try {
      await api.put("/api/v1/settings", {
        company_name: f.get("company_name"),
        policies: {
          retention_days: f.get("retention_days") || null, call_window_start: f.get("call_window_start") || null,
          call_window_end: f.get("call_window_end") || null, max_call_seconds: f.get("max_call_seconds") || null,
          max_attempts: f.get("max_attempts") || null, retry_interval_minutes: f.get("retry_interval_minutes") || null,
          recording_notice_confirmed: f.get("recording_notice_confirmed") === "on",
          recording_notice_text: f.get("recording_notice_text") || null,
          ai_external_transfer_confirmed: f.get("ai_external_transfer_confirmed") === "on",
        },
      });
      setOk(true); s.reload(); onChanged();
    } catch (e2) { setErr(e2 as ApiError); }
  }

  return (
    <form onSubmit={save} className="panel">
      {s.data.policy_problems.length > 0 && (
        <Notice kind="warn">실제 발신 전에 정할 항목: {s.data.policy_problems.join(" / ")}</Notice>
      )}
      <fieldset disabled={!admin}>
        <div className="grid3">
          <Field label="회사명(오프닝 안내에 사용)"><input name="company_name" defaultValue={s.data.organization.company_name} /></Field>
          <Field label="녹음·전사 보관 기간(일)" hint="법적 보관 기간은 담당자가 확정"><input name="retention_days" type="number" min={1} defaultValue={p.retention_days ?? ""} /></Field>
          <Field label="최대 통화 길이(초)"><input name="max_call_seconds" type="number" min={30} defaultValue={p.max_call_seconds ?? ""} /></Field>
          <Field label="통화 허용 시작(HH:MM)"><input name="call_window_start" type="time" defaultValue={p.call_window_start ?? ""} /></Field>
          <Field label="통화 허용 종료(HH:MM)"><input name="call_window_end" type="time" defaultValue={p.call_window_end ?? ""} /></Field>
          <Field label="최대 시도 횟수"><input name="max_attempts" type="number" min={1} defaultValue={p.max_attempts ?? ""} /></Field>
          <Field label="재시도 간격(분)"><input name="retry_interval_minutes" type="number" min={0} defaultValue={p.retry_interval_minutes ?? ""} /></Field>
        </div>
        <Field label="녹음 안내 문구(오프닝에 결합할 내용)"><textarea name="recording_notice_text" rows={2} defaultValue={p.recording_notice_text ?? ""} /></Field>
        <label className="inline"><input type="checkbox" name="recording_notice_confirmed" defaultChecked={!!p.recording_notice_confirmed} /> 녹음 안내·고지 방식을 담당자가 확정했습니다</label>
        <label className="inline"><input type="checkbox" name="ai_external_transfer_confirmed" defaultChecked={!!p.ai_external_transfer_confirmed} /> 예외 질문을 외부 AI로 보내는 범위를 담당자가 확정했습니다</label>
      </fieldset>
      {admin && (
        <div className="env-switch">
          <strong>조직 종류: {me.organization.environment === "sandbox" ? "시험 조직(MOCK 통화만)" : "운영 조직(실기기 A24만)"}</strong>
          <button type="button" className="btn" onClick={async () => {
            const to = me.organization.environment === "sandbox" ? "production" : "sandbox";
            if (!window.confirm(to === "production" ? "운영 조직으로 바꾸면 MOCK 단말로는 발신할 수 없고, 실기기 검증을 마친 A24만 발신합니다. 바꿀까요?" : "시험 조직으로 바꾸면 실기기 발신이 막히고 MOCK 통화만 됩니다. 바꿀까요?")) return;
            try { await api.put("/api/v1/settings", { environment: to }); onChanged(); s.reload(); } catch (e2) { setErr(e2 as ApiError); }
          }}>{me.organization.environment === "sandbox" ? "운영 조직(실통화)으로 전환" : "시험 조직으로 전환"}</button>
        </div>
      )}
      <p className="muted small">이 화면은 정책 값을 기록할 뿐 법률 적합성을 판단하지 않습니다. 허용 시간은 운영 정책이며 법적 허용시간으로 단정하지 않습니다.</p>
      <ErrorBox error={err} />
      {ok && <Notice kind="ok">저장했습니다.</Notice>}
      {admin && <div className="actions"><button className="btn primary">정책 저장</button></div>}
    </form>
  );
}

function Voice({ me }: { me: Me }) {
  const packs = useAsync(() => api.get<any[]>("/api/v1/voice-packs"));
  const [err, setErr] = useState<ApiError | null>(null);
  const [open, setOpen] = useState<string>("female");
  const admin = me.can.settings;
  const pack = (packs.data ?? []).find((p) => p.key === open);
  const [busy, setBusy] = useState(false);
  const [ttsMsg, setTtsMsg] = useState("");

  async function tts(engine: string) {
    if (!pack) return;
    setErr(null); setTtsMsg(""); setBusy(true);
    try {
      const r = await api.post(`/api/v1/voice-packs/${pack.key}/tts`, { engine });
      setTtsMsg(r.failed.length ? `일부 실패: ${r.generated}개 생성, ${r.failed.length}개 실패(${r.failed[0].error})` : `완료: ${r.generated}개 생성. 들어 보고 승인하세요.`);
      packs.reload();
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  async function run(fn: () => Promise<any>) {
    setErr(null);
    try { await fn(); packs.reload(); } catch (e) { setErr(e as ApiError); }
  }

  if ((packs.data ?? []).length === 0) {
    return (
      <div className="panel">
        <p>여성(김태희 실장)·남성 안내 음원팩을 기본 스크립트로 만듭니다. 녹음 파일을 올리고 승인하기 전까지는 실통화에 쓸 수 없습니다.</p>
        <ErrorBox error={err} />
        {admin && <button className="btn primary" onClick={() => run(() => api.post("/api/v1/voice-packs/init"))}>기본 음원팩 만들기</button>}
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="segmented" role="tablist">
        {(packs.data ?? []).map((p) => (
          <button key={p.key} role="tab" aria-selected={open === p.key} onClick={() => setOpen(p.key)}>
            {p.key === "female" ? "여성" : "남성"} · {p.speaker_name || "이름 미정"} v{p.version}
          </button>
        ))}
      </div>
      {pack && (
        <>
          <p>
            {pack.dialable ? <span className="badge ok">실통화 사용 가능</span> : <span className="badge warn">실통화 불가</span>}{" "}
            {pack.approved_by ? `승인: ${pack.approved_by}` : "승인 전"}
          </p>
          {!pack.dialable && <ul className="detail">{pack.problems.map((x: string) => <li key={x}>{x}</li>)}</ul>}
          <p className="muted small">‘김태희 실장’은 업무상 안내 역할 이름입니다. 특정 인물의 목소리를 흉내 내지 않고 승인된 녹음만 사용합니다. 녹음 파일은 WAV·MP3·FLAC 을 올리면 자동 변환됩니다.</p>
          {admin && (
            <div className="panel tts-box">
              <h3>자동 음성으로 한 번에 만들기</h3>
              <p className="muted small">아래 문구 13개와 날짜 조각 43개를 합성 음성으로 만듭니다. 오프닝의 [회사명]은 운영 정책의 회사명으로 바뀝니다.
                만든 뒤 몇 개를 들어 보고 ‘이 버전 승인’을 눌러야 실제 통화에 쓰입니다.</p>
              <div className="toolbar">
                <button className="btn primary" disabled={busy} onClick={() => tts("edge")}>
                  {busy ? "만드는 중… (1~2분)" : `온라인 음성(${pack.key === "female" ? "여성 SunHi" : "남성 InJoon"})으로 만들기`}
                </button>
                <button className="btn" disabled={busy} onClick={() => tts("sapi")}>Windows 내장 음성으로 만들기</button>
                {me.organization.environment === "sandbox" && <button className="btn ghost" disabled={busy} onClick={() => tts("test")}>시험용 톤으로 채우기</button>}
              </div>
              {ttsMsg && <Notice kind={ttsMsg.startsWith("완료") ? "ok" : "warn"}>{ttsMsg}</Notice>}
            </div>
          )}
          {admin && (
            <form className="toolbar" onSubmit={(e) => { e.preventDefault(); const n = new FormData(e.currentTarget).get("speaker") as string; run(() => api.put(`/api/v1/voice-packs/${pack.key}`, { speaker_name: n })); }}>
              <input name="speaker" defaultValue={pack.speaker_name} placeholder="안내 인물 이름" aria-label="안내 인물 이름" />
              <button className="btn">이름 저장</button>
              <span className="spacer" />
              <button type="button" className="btn primary" disabled={!pack.problems.every((x: string) => x.includes("승인"))} onClick={() => run(() => api.post(`/api/v1/voice-packs/${pack.key}/approve`))}>이 버전 승인</button>
            </form>
          )}
          <ErrorBox error={err} />
          <table className="table compact">
            <thead><tr><th>키</th><th>문구</th><th>녹음</th><th /></tr></thead>
            <tbody>
              {pack.required_keys.map((k: string) => {
                const c = pack.clips[k] ?? {};
                return (
                  <tr key={k}>
                    <td className="num">{k}</td>
                    <td>
                      {admin ? (
                        <form onSubmit={(e) => { e.preventDefault(); const t = new FormData(e.currentTarget).get("t") as string; run(() => api.put(`/api/v1/voice-packs/${pack.key}`, { texts: { [k]: t } })); }} className="inline-form">
                          <input name="t" defaultValue={c.text} aria-label={`${k} 문구`} className={/\[회사명\]|\[이름\]/.test(c.text) ? "has-placeholder" : ""} />
                          <button className="btn ghost small">문구 저장</button>
                        </form>
                      ) : c.text}
                    </td>
                    <td>{c.has_file ? <audio controls preload="none" src={`/api/v1/voice-packs/${pack.key}/clips/${k}`} /> : <span className="unknown">없음</span>}</td>
                    <td>{admin && <input type="file" accept=".wav,.mp3,.flac" aria-label={`${k} 녹음 올리기`} onChange={(e) => { const f = e.target.files?.[0]; if (f) run(() => api.upload(`/api/v1/voice-packs/${pack.key}/clips/${k}`, f)); }} />}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="muted small">날짜 재확인용 조각 음원 {pack.date_piece_keys.length}개(1월~12월, 1일~31일)도 필요합니다. 현재 녹음 없는 항목 {pack.missing_count}개.</p>
        </>
      )}
    </div>
  );
}

function Devices({ me }: { me: Me }) {
  const list = useAsync(() => api.get<any[]>("/api/v1/devices"), [], 5000);
  const [created, setCreated] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [proofFor, setProofFor] = useState<string | null>(null);
  const sandbox = me.organization.environment === "sandbox";
  const admin = me.can.settings;

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setErr(null);
    try {
      setCreated(await api.post("/api/v1/devices", { name: f.get("name"), kind: f.get("kind"), bind_local: f.get("bind") === "on" }));
      list.reload();
    } catch (e2) { setErr(e2 as ApiError); }
  }
  async function proof(e: FormEvent<HTMLFormElement>, id: string) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setErr(null);
    try {
      await api.post(`/api/v1/devices/${id}/proof`, {
        rx_verified: f.get("rx") === "on", tx_verified: f.get("tx") === "on", pc_silent_verified: f.get("silent") === "on",
        recording_verified: f.get("rec") === "on", test_calls: Number(f.get("calls") || 0),
        remote_listener: f.get("listener"), notes: f.get("notes") || "",
        environment: { cable: f.get("cable") || "", pc_os: f.get("pc_os") || "" },
      });
      setProofFor(null); list.reload();
    } catch (e2) { setErr(e2 as ApiError); }
  }

  return (
    <div>
      <div className="panel">
        <p>‘연결됨’은 PC 에이전트의 신호가 있다는 뜻일 뿐입니다. 실통화는 원격 단말로 상대 음성 수신·안내 음성 청취·PC 무음·녹음을 10회 이상 확인한 기록이 있어야 열립니다.</p>
        {(list.data ?? []).map((d) => (
          <div key={d.id} className="device">
            <div className="device-head">
              <strong>{d.name}</strong> <span className="muted">{d.kind === "mock" ? "MOCK 단말" : "갤럭시 A24"}</span>
              <span className={`badge ${d.connected ? "ok" : "muted"}`}>{d.connected ? "연결됨" : "연결 안 됨"}</span>
              {d.kind === "a24" && <span className={`badge ${d.real_call_ready ? "ok" : "warn"}`}>{d.real_call_ready ? "실통화 준비됨" : "실통화 미검증"}</span>}
              <span className="muted small">마지막 신호 {d.heartbeat_at ?? "없음"} · 단말 상태 {d.phone_state ?? "-"}</span>
            </div>
            {d.kind === "a24" && (
              <table className="table compact caps">
                <thead><tr><th>발신</th><th>종료</th><th>응답감지</th><th>RX</th><th>TX</th><th>녹음</th></tr></thead>
                <tbody><tr>{["dial", "hangup", "answer_detection", "rx_audio", "tx_audio", "recording"].map((k) => (
                  <td key={k} className={d.capabilities[k] === "supported" ? "ok" : "no"}>{({ supported: "보고됨", unsupported: "불가", unknown: "미확인" } as any)[d.capabilities[k] ?? "unknown"]}</td>
                ))}</tr></tbody>
              </table>
            )}
            {d.reasons?.length > 0 && <ul className="detail">{d.reasons.map((r: string) => <li key={r}>{r}</li>)}</ul>}
            {d.kind === "a24" && Object.keys(d.probe_report || {}).length > 0 && (
              <details><summary>aicc-probe 진단 결과</summary><pre className="pre">{JSON.stringify(d.probe_report, null, 2)}</pre></details>
            )}
            {d.kind === "a24" && admin && (proofFor === d.id ? (
              <form className="proof" onSubmit={(e) => proof(e, d.id)}>
                <p className="muted small">설계 11 실통화 시험 절차를 사람이 직접 수행한 결과만 체크하세요. 로그상 ‘전송됨’은 청취 증거가 아닙니다.</p>
                <label className="inline"><input type="checkbox" name="rx" /> PC 마이크 없이 원격 발화가 RX로 들어왔다</label>
                <label className="inline"><input type="checkbox" name="tx" /> 원격 청취자가 남녀 ARS 전체를 들었다</label>
                <label className="inline"><input type="checkbox" name="silent" /> 통화 중 PC 스피커·기본 출력에서 소리가 나지 않았다</label>
                <label className="inline"><input type="checkbox" name="rec" /> 녹음에 양쪽 발화가 실제 시각대로 남았다</label>
                <div className="grid3">
                  <Field label="승인된 시험 통화 횟수"><input name="calls" type="number" min={0} required /></Field>
                  <Field label="원격 청취자"><input name="listener" required /></Field>
                  <Field label="케이블·PC OS"><input name="cable" placeholder="케이블 종류" /><input name="pc_os" placeholder="Windows 11 등" /></Field>
                </div>
                <Field label="메모"><textarea name="notes" rows={2} /></Field>
                <div className="actions"><button className="btn primary">검증 결과 기록</button><button type="button" className="btn ghost" onClick={() => setProofFor(null)}>취소</button></div>
              </form>
            ) : <button className="btn ghost small" onClick={() => setProofFor(d.id)}>실통화 검증 결과 기록</button>)}
          </div>
        ))}
        <ErrorBox error={err} />
      </div>
      {admin && (
        <form className="panel" onSubmit={create}>
          <h2>단말 등록</h2>
          <div className="grid3">
            <Field label="이름"><input name="name" required defaultValue={sandbox ? "MOCK 단말" : "A24-1"} /></Field>
            <Field label="종류">
              <select name="kind" defaultValue={sandbox ? "mock" : "a24"}>
                <option value="mock" disabled={!sandbox}>MOCK(시험 조직 전용)</option>
                <option value="a24">갤럭시 A24</option>
              </select>
            </Field>
            <label className="inline"><input type="checkbox" name="bind" defaultChecked /> 이 PC의 AICC 에이전트에 바로 연결</label>
          </div>
          <div className="actions"><button className="btn primary">등록</button></div>
          {created && (
            <Notice kind={created.bound_local ? "ok" : "warn"}>
              {created.bound_local ? "이 PC 에이전트에 연결했습니다. 몇 초 뒤 ‘연결됨’으로 바뀝니다." : <>장치 토큰(다시 표시되지 않음): <code className="token">{created.token}</code></>}
            </Notice>
          )}
        </form>
      )}
    </div>
  );
}

function Users({ me }: { me: Me }) {
  const list = useAsync(() => api.get<any[]>("/api/v1/users"));
  const [err, setErr] = useState<ApiError | null>(null);
  async function add(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setErr(null);
    try {
      await api.post("/api/v1/users", {
        username: f.get("username"), password: f.get("password"), role: f.get("role"),
        perms: { play_recording: f.get("p_play") === "on", edit_dates: f.get("p_edit") === "on", export_all: f.get("p_export") === "on" },
      });
      (e.target as HTMLFormElement).reset(); list.reload();
    } catch (e2) { setErr(e2 as ApiError); }
  }
  if (!me.can.settings) return <p className="muted">관리자만 볼 수 있습니다.</p>;
  return (
    <div className="panel">
      <table className="table compact">
        <thead><tr><th>아이디</th><th>역할</th><th>별도 권한</th><th>상태</th></tr></thead>
        <tbody>{(list.data ?? []).map((u) => (
          <tr key={u.id}><td>{u.username}</td><td>{({ admin: "관리자", operator: "운영자", reviewer: "검수자" } as any)[u.role]}</td>
            <td>{Object.entries(u.perms).filter(([, v]) => v).map(([k]) => ({ play_recording: "녹음 재생", edit_dates: "날짜 수정", export_all: "전체 내려받기" } as any)[k] ?? k).join(", ") || "-"}</td>
            <td>{u.active ? "사용" : "중지"}</td></tr>
        ))}</tbody>
      </table>
      <form onSubmit={add}>
        <h2>사용자 추가</h2>
        <div className="grid3">
          <Field label="아이디"><input name="username" required minLength={3} /></Field>
          <Field label="초기 비밀번호(10자 이상)"><input name="password" type="password" required minLength={10} autoComplete="new-password" /></Field>
          <Field label="역할"><select name="role"><option value="operator">운영자</option><option value="reviewer">검수자</option><option value="admin">관리자</option></select></Field>
        </div>
        <label className="inline"><input type="checkbox" name="p_play" /> 녹음 재생</label>
        <label className="inline"><input type="checkbox" name="p_edit" /> 날짜 수정</label>
        <label className="inline"><input type="checkbox" name="p_export" /> 전체 내려받기</label>
        <ErrorBox error={err} />
        <div className="actions"><button className="btn primary">추가</button></div>
      </form>
    </div>
  );
}

function Audit() {
  const list = useAsync(() => api.get<any[]>("/api/v1/audit?limit=300"));
  if (list.error) return <ErrorBox error={list.error} />;
  return (
    <div className="panel">
      <table className="table compact">
        <thead><tr><th>시각(KST)</th><th>주체</th><th>행동</th><th>대상</th></tr></thead>
        <tbody>{(list.data ?? []).map((a, i) => <tr key={i}><td className="num">{a.at}</td><td>{a.actor}</td><td>{a.action}</td><td className="num small">{a.resource}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

export default function SettingsPage({ me, tab, onChanged }: { me: Me; tab: string; onChanged: () => void }) {
  return (
    <Page title="설정과 진단">
      <div className="tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => go("/settings", { tab: k })}>{label}</button>
        ))}
      </div>
      {tab === "policy" && <Policy me={me} onChanged={onChanged} />}
      {tab === "voice" && <Voice me={me} />}
      {tab === "devices" && <Devices me={me} />}
      {tab === "users" && <Users me={me} />}
      {tab === "audit" && <Audit />}
    </Page>
  );
}
