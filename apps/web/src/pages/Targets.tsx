import { useMemo, useState } from "react";
import { api, ApiError, ERROR_TEXT, Me, OUTCOME } from "../api";
import { go } from "../App";
import { DateCell, Empty, ErrorBox, Field, Notice, Page, useAsync } from "../ui";

export default function TargetsPage({ me, batchId }: { me: Me; batchId: string | null }) {
  const imports = useAsync(() => api.get<any[]>("/api/v1/imports"));
  const committed = (imports.data ?? []).filter((b) => b.status === "COMMITTED");
  const batch = batchId || committed[0]?.id || null;
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("all");
  const list = useAsync(
    () => (batch ? api.get<any>(`/api/v1/caregivers?batch_id=${batch}&limit=1000${filter !== "all" ? `&status=${filter}` : ""}${q ? `&q=${encodeURIComponent(q)}` : ""}`) : Promise.resolve(null)),
    [batch, filter, q],
  );
  const packs = useAsync(() => api.get<any[]>("/api/v1/voice-packs"));
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [pack, setPack] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const items: any[] = list.data?.items ?? [];
  const dialable = useMemo(() => items.filter((i) => i.dialable), [items]);
  const meta = committed.find((b) => b.id === batch);

  function toggle(id: string) {
    const s = new Set(sel);
    s.has(id) ? s.delete(id) : s.add(id);
    setSel(s);
  }

  async function create() {
    setErr(null); setBusy(true);
    try {
      const r = await api.post("/api/v1/campaigns", { batch_id: batch, target_ids: [...sel], voice_pack_id: pack, name: name || undefined }, true);
      go("/prepare", { campaign: r.campaign_id });
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  if (!batch) return <Page title="대상 선택"><Empty>저장된 명부가 없습니다. <a href="#/upload">명부 업로드</a>에서 먼저 올리세요.</Empty></Page>;

  return (
    <Page title="대상 선택" actions={
      <select aria-label="명부" value={batch} onChange={(e) => { setSel(new Set()); go("/targets", { batch: e.target.value }); }}>
        {committed.map((b) => <option key={b.id} value={b.id}>{b.file_name} ({b.created_at})</option>)}
      </select>
    }>
      {meta && <p className="muted">근무확정일 열 의미: {meta.date_semantics === "NEXT_AVAILABLE" ? "다음 근무 가능일(통화 확인 시 갱신)" : "실제 배정 확정일(갱신 안 함)"}</p>}
      <div className="toolbar">
        <input type="search" placeholder="이름·간병인 번호·연락처 끝 4자리" value={q} onChange={(e) => setQ(e.target.value)} aria-label="검색" />
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="상태 필터">
          <option value="all">전체</option><option value="dialable">발신 가능</option><option value="excluded">제외됨</option>
        </select>
        <span className="spacer" />
        <button className="btn ghost" onClick={() => setSel(new Set(dialable.map((i) => i.caregiver_id)))}>발신 가능 {dialable.length}명 모두 선택</button>
        <button className="btn ghost" onClick={() => setSel(new Set())}>선택 해제</button>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th aria-label="선택" /><th>행</th><th>간병인 번호</th><th>이름</th><th>연락처</th><th>근무확정일</th><th>최근 통화</th><th>상태</th></tr></thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.row_no} className={i.dialable ? "" : "dim"}>
                <td>
                  <input type="checkbox" aria-label={`${i.name ?? "행 " + i.row_no} 선택`} disabled={!i.dialable || !me.can.upload_dial}
                    checked={i.caregiver_id ? sel.has(i.caregiver_id) : false} onChange={() => toggle(i.caregiver_id)} />
                </td>
                <td className="num">{i.row_no}</td>
                <td className="num">{i.external_no}</td>
                <td>{i.name}</td>
                <td className="num">{i.phone}</td>
                <td><DateCell value={i.confirmed_available_date} /></td>
                <td>{i.last_outcome ? `${OUTCOME[i.last_outcome] ?? i.last_outcome} · ${i.last_call_at}` : <span className="muted">없음</span>}</td>
                <td>
                  {i.blocked ? <span className="badge danger">연락거부</span>
                    : i.exclude_reason ? <span className="badge warn">{ERROR_TEXT[i.exclude_reason] ?? i.exclude_reason}{i.errors.length ? `: ${i.errors.map((e: string) => ERROR_TEXT[e] ?? e).join(", ")}` : ""}</span>
                    : <span className="badge muted">발신 가능</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {me.can.upload_dial && (
        <div className="panel sticky-bottom">
          <div className="grid3">
            <Field label="안내 음성">
              <select value={pack} onChange={(e) => setPack(e.target.value)}>
                <option value="">선택</option>
                {(packs.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>{p.key === "female" ? "여성" : "남성"} · {p.speaker_name || "이름 미정"} · v{p.version}{p.dialable ? "" : " (실통화 불가)"}</option>
                ))}
              </select>
            </Field>
            <Field label="캠페인 이름(선택)"><input value={name} onChange={(e) => setName(e.target.value)} placeholder={meta?.file_name} /></Field>
            <div className="field">
              <span className="field-label">선택 인원</span>
              <strong className="big-num">{sel.size}명</strong>
            </div>
          </div>
          {(packs.data ?? []).length === 0 && <Notice kind="warn">음원팩이 없습니다. 관리자가 <a href="#/settings?tab=voice">설정 &gt; 안내 음성</a>에서 만들어야 합니다.</Notice>}
          <ErrorBox error={err} />
          <div className="actions">
            <button className="btn primary" disabled={busy || sel.size === 0 || !pack} onClick={create}>
              {sel.size}명으로 통화 준비
            </button>
          </div>
        </div>
      )}
    </Page>
  );
}
