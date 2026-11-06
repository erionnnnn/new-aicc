import { useState } from "react";
import { api, ApiError, Me } from "../api";
import { Empty, ErrorBox, OutcomeBadge, Page, useAsync } from "../ui";

function Play({ r }: { r: any }) {
  const [url, setUrl] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  if (r.state !== "READY") return <span className="badge warn">{({ FAILED: "저장 실패", EXPIRED: "보관기간 만료", PENDING: "업로드 대기" } as any)[r.state]}</span>;
  if (!r.call_ended) return <button className="btn ghost small" disabled>통화 중</button>;
  if (!r.can_play) return <span className="muted">권한 없음</span>;
  if (url) return <audio controls src={url} preload="none" />;
  return (
    <>
      <button className="btn ghost small" onClick={async () => {
        try { setUrl((await api.post(`/api/v1/recordings/${r.id}/playback`)).url); } catch (e) { setErr(e as ApiError); }
      }}>재생</button>
      <ErrorBox error={err} />
    </>
  );
}

export default function RecordingsPage({ me }: { me: Me }) {
  const list = useAsync(() => api.get<any[]>("/api/v1/recordings"));
  const items = list.data ?? [];
  return (
    <Page title="녹음 목록">
      <p className="muted">녹음은 자동으로 재생되지 않습니다. 재생은 기록이 남으며, 헤드셋 사용을 권장합니다. 왼쪽 채널은 상대, 오른쪽 채널은 안내 음성입니다.</p>
      {!me.can.play_recording && <p className="muted">이 계정은 녹음 재생 권한이 없습니다.</p>}
      {items.length === 0 ? <Empty>녹음이 없습니다.</Empty> : (
        <table className="table">
          <thead><tr><th>통화일(KST)</th><th>대상</th><th>결과</th><th>길이</th><th>보관 만료</th><th>재생</th></tr></thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id}>
                <td className="num">{r.created_at}</td>
                <td>{r.name} <span className="num muted">{r.external_no}</span> {r.is_mock && <span className="badge warn">MOCK</span>}</td>
                <td><OutcomeBadge outcome={r.outcome} /></td>
                <td className="num">{Math.round(r.duration_ms / 1000)}초</td>
                <td className="num">{r.expires_at ?? <span className="muted">정책 미설정</span>}</td>
                <td><Play r={r} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  );
}
