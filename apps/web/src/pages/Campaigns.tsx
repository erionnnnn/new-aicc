import { api } from "../api";
import { Empty, Page, useAsync } from "../ui";

const STATE: Record<string, string> = {
  DRAFT: "준비", RUNNING: "진행 중", PAUSED: "일시정지", STOPPING: "종료 확인 중", STOPPED: "중단됨", COMPLETED: "완료",
};

export default function CampaignsPage() {
  const list = useAsync(() => api.get<any[]>("/api/v1/campaigns"), [], 5000);
  const items = list.data ?? [];
  return (
    <Page title="캠페인">
      {items.length === 0 ? <Empty>캠페인이 없습니다. <a href="#/targets">대상 선택</a>에서 만드세요.</Empty> : (
        <table className="table">
          <thead><tr><th>이름</th><th>상태</th><th>진행</th><th>만든 시각(KST)</th><th>데이터</th><th /></tr></thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{STATE[c.state] ?? c.state}</td>
                <td className="num">{c.done} / {c.total}</td>
                <td className="num">{c.created_at}</td>
                <td>{c.is_mock ? <span className="badge warn">MOCK 시험</span> : c.pilot ? <span className="badge warn">실통화 시험</span> : <span className="badge muted">실통화</span>}</td>
                <td className="row-links">
                  {c.state === "DRAFT" || c.state === "PAUSED" ? <a href={`#/prepare?campaign=${c.id}`}>통화 준비</a> : null}
                  <a href={`#/monitor?campaign=${c.id}`}>통화 진행</a>
                  <a href={`#/export?campaign=${c.id}`}>결과</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  );
}
