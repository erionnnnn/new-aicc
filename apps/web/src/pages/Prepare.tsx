import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { go } from "../App";
import { ErrorBox, Field, Notice, Page, useAsync } from "../ui";

export default function PreparePage({ me, campaignId }: { me: Me; campaignId: string }) {
  const camp = useAsync(() => api.get<any>(`/api/v1/campaigns/${campaignId}`), [campaignId]);
  const devices = useAsync(() => api.get<any[]>("/api/v1/devices"), [], 4000);
  const [device, setDevice] = useState<string>("");
  const dev = device || camp.data?.device_id || devices.data?.[0]?.id || "";
  const pf = useAsync(() => api.get<any>(`/api/v1/campaigns/${campaignId}/preflight${dev ? `?device_id=${dev}` : ""}`), [campaignId, dev], 4000);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const c = camp.data;

  async function start() {
    setErr(null); setBusy(true);
    try {
      await api.post(`/api/v1/campaigns/${campaignId}/start`, { device_id: dev, expected_version: c.version }, true);
      go("/monitor", { campaign: campaignId });
    } catch (e) { setErr(e as ApiError); camp.reload(); pf.reload(); } finally { setBusy(false); }
  }

  if (!c) return <Page title="통화 준비"><ErrorBox error={camp.error} /></Page>;
  const queued = c.target_states?.QUEUED ?? 0;
  return (
    <Page title="통화 준비">
      <div className="panel">
        <dl className="stats">
          <div><dt>캠페인</dt><dd className="text">{c.name}</dd></div>
          <div><dt>발신 대기</dt><dd>{queued}명</dd></div>
          <div><dt>안내 음성</dt><dd className="text">{c.voice_pack ? `${c.voice_pack.key === "female" ? "여성" : "남성"} v${c.voice_pack.version}` : "-"}</dd></div>
          <div><dt>데이터</dt><dd className="text">{c.is_mock ? "MOCK 시험" : c.pilot ? "실통화 시험(시험 번호만)" : "실통화"}</dd></div>
        </dl>
        <Field label="발신 단말">
          <select value={dev} onChange={(e) => setDevice(e.target.value)}>
            {(devices.data ?? []).length === 0 && <option value="">등록된 단말 없음</option>}
            {(devices.data ?? []).map((d) => (
              <option key={d.id} value={d.id}>{d.name} ({d.kind === "mock" ? "MOCK" : "갤럭시 A24"}) — {d.connected ? "연결됨" : "연결 안 됨"}</option>
            ))}
          </select>
        </Field>
        {(devices.data ?? []).length === 0 && <Notice kind="warn">단말이 없습니다. 관리자가 <a href="#/settings?tab=devices">설정 &gt; 단말</a>에서 등록해야 합니다.</Notice>}
      </div>

      <div className="panel">
        <h2>시작 조건</h2>
        <ul className="checklist">
          {(pf.data?.checks ?? []).map((ch: any) => (
            <li key={ch.key} className={ch.ok ? "ok" : "no"}>
              <span className="mark" aria-hidden>{ch.ok ? "✓" : "✕"}</span>
              <div>
                <div>{ch.label}</div>
                {!ch.ok && ch.detail.length > 0 && <ul className="detail">{ch.detail.map((d: string) => <li key={d}>{d}</li>)}</ul>}
              </div>
            </li>
          ))}
        </ul>
        {!c.is_mock && <p className="muted">‘USB 연결됨’만으로는 시작할 수 없습니다. 원격 단말로 RX·TX·무음·녹음을 확인한 실통화 검증 기록이 있어야 합니다.</p>}
        <ErrorBox error={err} />
        <div className="actions">
          <button className="btn primary" disabled={busy || !pf.data?.ok || !me.can.upload_dial || !["DRAFT", "PAUSED"].includes(c.state)} onClick={start}>
            {c.state === "PAUSED" ? "재개하기" : `${queued}명에게 순차 발신 시작`}
          </button>
          {!pf.data?.ok && <span className="muted">위 조건을 모두 충족해야 시작할 수 있습니다.</span>}
        </div>
      </div>
    </Page>
  );
}
