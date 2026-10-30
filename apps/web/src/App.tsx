import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, Me } from "./api";
import { ErrorBox, Field, MockBanner, useAsync } from "./ui";
import UploadPage from "./pages/Upload";
import TargetsPage from "./pages/Targets";
import PreparePage from "./pages/Prepare";
import MonitorPage from "./pages/Monitor";
import ReviewPage from "./pages/Review";
import RecordingsPage from "./pages/Recordings";
import ExportPage from "./pages/Export";
import BlocksPage from "./pages/Blocks";
import SettingsPage from "./pages/Settings";
import CampaignsPage from "./pages/Campaigns";
import ScenariosPage from "./pages/Scenarios";

export type Route = { path: string; params: URLSearchParams };

function parseHash(): Route {
  const h = window.location.hash.replace(/^#/, "") || "/upload";
  const [path, q] = h.split("?");
  return { path, params: new URLSearchParams(q || "") };
}

export function go(path: string, params: Record<string, string> = {}) {
  const q = new URLSearchParams(params).toString();
  window.location.hash = q ? `${path}?${q}` : path;
}

const NAV: { group: string; items: { path: string; label: string }[] }[] = [
  { group: "명부", items: [{ path: "/upload", label: "명부 업로드" }, { path: "/targets", label: "대상 선택" }, { path: "/blocks", label: "연락거부" }] },
  { group: "통화", items: [{ path: "/campaigns", label: "캠페인" }] },
  { group: "결과", items: [{ path: "/review", label: "결과 검수" }, { path: "/export", label: "결과 다운로드" }] },
  { group: "녹음", items: [{ path: "/recordings", label: "녹음 목록" }] },
  { group: "대화", items: [{ path: "/scenarios", label: "시나리오" }] },
  { group: "설정", items: [{ path: "/settings", label: "설정과 진단" }] },
];

function StatusBar({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const devices = useAsync(() => api.get<any[]>("/api/v1/devices"), [], 5000);
  const live = useAsync(() => api.get<any[]>("/api/v1/campaigns"), [], 5000);
  const ds = devices.data ?? [];
  const connected = ds.some((d) => d.connected);
  const ready = ds.some((d) => d.real_call_ready);
  const running = (live.data ?? []).find((c) => c.state === "RUNNING" || c.state === "STOPPING");
  const mock = me.organization.environment === "sandbox";
  return (
    <header className="statusbar">
      <div className="brand">일정 확인 AICC</div>
      <div className="status-items" aria-live="polite">
        <span className={`pill ${connected ? "on" : "off"}`}>
          {mock ? "MOCK 에이전트" : "단말 연결"} {connected ? "연결됨" : "연결 안 됨"}
        </span>
        <span className={`pill ${ready ? "on" : "off"}`}>
          실통화 {mock ? "불가(시험 조직)" : ready ? "준비됨" : "미검증"}
        </span>
        {running ? (
          <a className="pill live" href={`#/monitor?campaign=${running.id}`}>
            진행 중 {running.done}/{running.total}
          </a>
        ) : (
          <span className="pill">진행 중 통화 없음</span>
        )}
      </div>
      <div className="account">
        <span>{me.username}</span>
        <span className="role">{({ admin: "관리자", operator: "운영자", reviewer: "검수자" } as any)[me.role]}</span>
        <button className="btn ghost small" onClick={onLogout}>로그아웃</button>
      </div>
    </header>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const status = useAsync(() => api.get<{ bootstrapped: boolean }>("/api/v1/auth/status"));
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const boot = status.data && !status.data.bootstrapped;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setErr(null);
    try {
      if (boot) {
        await api.post("/api/v1/auth/bootstrap", {
          org_name: f.get("org_name"), environment: f.get("environment"),
          username: f.get("username"), password: f.get("password"),
        });
      }
      await api.post("/api/v1/auth/login", { username: f.get("username"), password: f.get("password") });
      onDone();
    } catch (e2) {
      setErr(e2 as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (!status.data) return <div className="login-wrap"><ErrorBox error={status.error} /></div>;
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>{boot ? "처음 설정" : "로그인"}</h1>
        <p className="muted">
          {boot ? "관리자 계정과 조직을 만듭니다. 실제 발신 전에는 시험 조직으로 시작하세요." : "간병사 근무 일정 확인 업무"}
        </p>
        {boot && (
          <>
            <Field label="조직 이름"><input name="org_name" required defaultValue="유니메오" /></Field>
            <fieldset className="radio-row">
              <legend>조직 종류</legend>
              <label><input type="radio" name="environment" value="sandbox" defaultChecked /> 시험 조직(MOCK 통화만)</label>
              <label><input type="radio" name="environment" value="production" /> 운영 조직(실기기만)</label>
            </fieldset>
          </>
        )}
        <Field label="아이디"><input name="username" autoComplete="username" required minLength={3} /></Field>
        <Field label="비밀번호" hint={boot ? "10자 이상" : undefined}>
          <input name="password" type="password" autoComplete={boot ? "new-password" : "current-password"} required minLength={boot ? 10 : 1} />
        </Field>
        <ErrorBox error={err} />
        <button className="btn primary" disabled={busy}>{boot ? "만들고 로그인" : "로그인"}</button>
      </form>
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash());
  const me = useAsync<Me | null>(async () => {
    try {
      return await api.get<Me>("/api/v1/auth/me");
    } catch (e) {
      if ((e as ApiError).status === 401) return null;
      throw e;
    }
  });

  useEffect(() => {
    const f = () => setRoute(parseHash());
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);

  if (me.loading && !me.data) return <div className="login-wrap">불러오는 중…</div>;
  if (!me.data) return <Login onDone={me.reload} />;
  const user = me.data;

  const page = (() => {
    const p = route.params;
    switch (route.path) {
      case "/upload": return <UploadPage me={user} />;
      case "/targets": return <TargetsPage me={user} batchId={p.get("batch")} />;
      case "/campaigns": return <CampaignsPage />;
      case "/prepare": return <PreparePage me={user} campaignId={p.get("campaign")!} />;
      case "/monitor": return <MonitorPage me={user} campaignId={p.get("campaign")!} />;
      case "/review": return <ReviewPage me={user} callId={p.get("call")} />;
      case "/recordings": return <RecordingsPage me={user} />;
      case "/export": return <ExportPage me={user} campaignId={p.get("campaign")} />;
      case "/blocks": return <BlocksPage me={user} />;
      case "/scenarios": return <ScenariosPage me={user} tab={p.get("tab") || "proposals"} />;
      case "/settings": return <SettingsPage me={user} tab={p.get("tab") || "policy"} onChanged={me.reload} />;
      default: return <UploadPage me={user} />;
    }
  })();

  return (
    <div className="shell">
      <StatusBar me={user} onLogout={async () => { await api.post("/api/v1/auth/logout"); me.reload(); }} />
      <nav className="sidenav" aria-label="주 메뉴">
        {NAV.map((g) => (
          <div key={g.group} className="nav-group">
            <div className="nav-group-name">{g.group}</div>
            {g.items.map((it) => (
              <a key={it.path} href={`#${it.path}`} aria-current={route.path === it.path ? "page" : undefined}>
                {it.label}
              </a>
            ))}
          </div>
        ))}
      </nav>
      <main className="content">
        {user.organization.environment === "sandbox" && <MockBanner />}
        {page}
      </main>
    </div>
  );
}
