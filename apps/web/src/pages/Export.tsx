import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { ErrorBox, Field, Notice, Page, useAsync } from "../ui";

export default function ExportPage({ me, campaignId }: { me: Me; campaignId: string | null }) {
  const camps = useAsync(() => api.get<any[]>("/api/v1/campaigns"));
  const imports = useAsync(() => api.get<any[]>("/api/v1/imports"));
  const [source, setSource] = useState<string>(campaignId ? `c:${campaignId}` : "");
  const [scope, setScope] = useState(me.can.export_all ? "all" : "selected");
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const src = source || (camps.data?.[0] ? `c:${camps.data[0].id}` : "");

  async function make() {
    setErr(null); setBusy(true); setResult(null);
    try {
      const body = src.startsWith("c:") ? { campaign_id: src.slice(2), scope } : { batch_id: src.slice(2), scope: "all" };
      const r = await api.post("/api/v1/exports", body, true);
      setResult(await api.get(`/api/v1/exports/${r.export_id}`));
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  return (
    <Page title="결과 다운로드">
      <div className="panel">
        <p className="lead">원본 명부의 행 순서와 값을 그대로 두고, 상대가 재확인한 날짜만 반영한 엑셀을 만듭니다.
          시트는 명부·통화이력·변경이력·오류행·요약입니다. 수식은 값으로 바뀌고 서식은 일부만 유지됩니다.</p>
        <div className="grid3">
          <Field label="대상">
            <select value={src} onChange={(e) => setSource(e.target.value)}>
              <optgroup label="캠페인">
                {(camps.data ?? []).map((c) => <option key={c.id} value={`c:${c.id}`}>{c.name} ({c.done}/{c.total}{c.is_mock ? ", MOCK" : ""})</option>)}
              </optgroup>
              <optgroup label="명부 전체(모든 캠페인)">
                {(imports.data ?? []).filter((b) => b.status === "COMMITTED").map((b) => <option key={b.id} value={`b:${b.id}`}>{b.file_name}</option>)}
              </optgroup>
            </select>
          </Field>
          {src.startsWith("c:") && (
            <Field label="범위">
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="all" disabled={!me.can.export_all}>전체 행{me.can.export_all ? "" : " (권한 필요)"}</option>
                <option value="selected">캠페인 대상 행만 결과 표시</option>
              </select>
            </Field>
          )}
        </div>
        <ErrorBox error={err} />
        <div className="actions"><button className="btn primary" disabled={busy || !src} onClick={make}>엑셀 만들기</button></div>
      </div>
      {result && (
        <div className="panel">
          {result.in_progress && <Notice kind="warn">통화가 아직 진행 중이라 ‘진행 중 결과’로 표시된 파일입니다. 최종 결과가 아닙니다.</Notice>}
          <dl className="stats">
            {Object.entries(result.summary).map(([k, v]) => <div key={k}><dt>{k}</dt><dd className={typeof v === "number" ? "" : "text"}>{String(v)}</dd></div>)}
          </dl>
          <p className="muted small">업로드 행 수와 통화·변경 건수를 맞춰 보세요(일일 업무 종료 대사).</p>
          <a className="btn primary" href={`/api/v1/exports/${result.id}/download`}>결과 엑셀 내려받기</a>
        </div>
      )}
    </Page>
  );
}
