import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { Empty, ErrorBox, Field, Notice, Page, useAsync } from "../ui";

export default function BlocksPage({ me }: { me: Me }) {
  const [q, setQ] = useState("");
  const [revoked, setRevoked] = useState(false);
  const list = useAsync(() => api.get<any[]>(`/api/v1/contact-blocks?include_revoked=${revoked}${q ? `&q=${encodeURIComponent(q)}` : ""}`), [q, revoked]);
  const [target, setTarget] = useState<any>(null);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [msg, setMsg] = useState("");

  async function revoke() {
    setErr(null);
    try {
      await api.post(`/api/v1/contact-blocks/${target.id}/revoke`, { reason });
      setMsg("차단을 해제했습니다. 기존 기록은 남아 있습니다.");
      setTarget(null); setReason(""); list.reload();
    } catch (e) { setErr(e as ApiError); }
  }

  return (
    <Page title="연락거부 관리">
      <p className="muted">통화 중 ‘연락하지 말라’는 요청은 즉시 저장되며, 같은 번호는 다시 올려도 업로드·대기열·발신 직전에 모두 제외됩니다.
        통화 밖에서 받은 연락거부는 <a href="#/review?state=ALL">결과 검수</a>의 통화 상세에서 등록합니다.</p>
      <div className="toolbar">
        <input type="search" placeholder="이름·간병인 번호·연락처" value={q} onChange={(e) => setQ(e.target.value)} aria-label="검색" />
        <label className="inline"><input type="checkbox" checked={revoked} onChange={(e) => setRevoked(e.target.checked)} /> 해제 이력 포함</label>
      </div>
      {msg && <Notice kind="ok">{msg}</Notice>}
      {(list.data ?? []).length === 0 ? <Empty>연락거부 기록이 없습니다.</Empty> : (
        <table className="table">
          <thead><tr><th>대상</th><th>연락처</th><th>사유</th><th>차단 시각(KST)</th><th>등록</th><th>해제</th></tr></thead>
          <tbody>
            {list.data!.map((b) => (
              <tr key={b.id} className={b.revoked_at ? "dim" : ""}>
                <td>{b.caregiver_name} <span className="num muted">{b.external_no}</span></td>
                <td className="num">{b.phone}</td>
                <td>{b.reason}{b.source_call_id && <> · <a href={`#/review?call=${b.source_call_id}`}>통화 보기</a></>}</td>
                <td className="num">{b.blocked_at}</td>
                <td>{b.blocked_by}</td>
                <td>{b.revoked_at ? <span>{b.revoked_at} {b.revoked_by}: {b.revoke_reason}</span>
                  : me.can.unblock_contact ? <button className="btn ghost small" onClick={() => { setTarget(b); setMsg(""); }}>해제</button>
                  : <span className="muted">관리자만</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {target && (
        <div className="panel">
          <h2>{target.caregiver_name} 차단 해제</h2>
          <Notice kind="warn">해제하면 다음 캠페인부터 다시 발신 대상이 될 수 있습니다. 본인의 재동의 같은 근거를 남겨야 합니다.</Notice>
          <Field label="해제 사유와 근거(5자 이상)"><textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          <ErrorBox error={err} />
          <div className="actions">
            <button className="btn danger" disabled={reason.trim().length < 5} onClick={revoke}>차단 해제</button>
            <button className="btn ghost" onClick={() => setTarget(null)}>취소</button>
          </div>
        </div>
      )}
    </Page>
  );
}
