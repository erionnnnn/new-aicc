import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { ErrorBox, Field, Notice, Page, useAsync } from "../ui";

function HourlyStats({ campaignId }: { campaignId: string }) {
  const [days, setDays] = useState(30);
  const [scope, setScope] = useState<"all" | "camp">("all");
  const q = `/api/v1/stats/hourly?days=${days}` + (scope === "camp" && campaignId ? `&campaign_id=${campaignId}` : "");
  const st = useAsync(() => api.get<any>(q), [q]);
  const d = st.data;
  const best = d?.hours?.filter((h: any) => h.calls >= 5).sort((a: any, b: any) => (b.connect_rate ?? 0) - (a.connect_rate ?? 0))[0];
  const pct = (v: number | null) => (v == null ? "-" : `${v}%`);
  return (
    <div className="panel">
      <h2>시간대별 연결률·응답률</h2>
      <p className="lead">몇 시에 걸면 잘 받는지 봅니다(발신 시각 기준, 한국 시간). 캠페인 시작 시간을 정할 때 참고하세요. 모의 통화는 빠집니다.</p>
      <div className="grid3">
        <Field label="기간">
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>최근 7일</option><option value={30}>최근 30일</option><option value={90}>최근 90일</option>
          </select>
        </Field>
        <Field label="범위">
          <select value={scope} onChange={(e) => setScope(e.target.value as any)}>
            <option value="all">모든 캠페인</option>
            <option value="camp" disabled={!campaignId}>선택한 캠페인만</option>
          </select>
        </Field>
      </div>
      <ErrorBox error={st.error} />
      {d && (d.hours.length === 0 ? <p className="muted">이 기간에 끝난 실제 통화가 없습니다.</p> : (
        <>
          {best && <Notice kind="ok">연결률이 가장 높은 시간: {best.hour}시대 ({pct(best.connect_rate)}, {best.calls}건)</Notice>}
          <div className="table-wrap">
            <table className="table compact">
              <thead><tr><th>시간</th><th>발신</th><th>연결(받음)</th><th>연결률</th><th>응답</th><th>응답률</th><th>날짜 확정</th><th>확정률</th></tr></thead>
              <tbody>
                {d.hours.map((h: any) => (
                  <tr key={h.hour} className={h.calls < 5 ? "dim" : ""}>
                    <td className="num">{String(h.hour).padStart(2, "0")}:00~{String(h.hour).padStart(2, "0")}:59</td>
                    <td className="num">{h.calls}</td><td className="num">{h.connected}</td><td className="num">{pct(h.connect_rate)}</td>
                    <td className="num">{h.responded}</td><td className="num">{pct(h.response_rate)}</td>
                    <td className="num">{h.confirmed}</td><td className="num">{pct(h.confirm_rate)}</td>
                  </tr>
                ))}
                <tr><td><b>합계</b></td><td className="num">{d.total.calls}</td><td className="num">{d.total.connected}</td><td className="num">{pct(d.total.connect_rate)}</td>
                  <td className="num">{d.total.responded}</td><td className="num">{pct(d.total.response_rate)}</td>
                  <td className="num">{d.total.confirmed}</td><td className="num">{pct(d.total.confirm_rate)}</td></tr>
              </tbody>
            </table>
          </div>
          <p className="muted small">연결 = 사람이 받음(무응답·통화중·발신 실패·음성사서함 제외). 응답 = 날짜·거절·재연락 등 의미 있는 답을 함. 5건 미만 시간대는 흐리게 표시(참고만).</p>
        </>
      ))}
    </div>
  );
}

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
      <HourlyStats campaignId={src.startsWith("c:") ? src.slice(2) : ""} />
    </Page>
  );
}
