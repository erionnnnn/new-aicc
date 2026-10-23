import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, OUTCOME } from "./api";

export function useAsync<T>(fn: () => Promise<T>, deps: any[] = [], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    reload();
    if (!pollMs) return;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

export function ErrorBox({ error }: { error: any }) {
  if (!error) return null;
  const checks = error?.details?.checks as any[] | undefined;
  const problems = error?.details?.problems as string[] | undefined;
  return (
    <div className="alert danger" role="alert">
      <strong>{error.message}</strong>
      {checks && (
        <ul>{checks.filter((c) => !c.ok).map((c) => <li key={c.key}>{c.label}: {c.detail.join(", ") || "미충족"}</li>)}</ul>
      )}
      {problems && <ul>{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
    </div>
  );
}

export function Notice({ kind = "info", children }: { kind?: "info" | "warn" | "danger" | "ok"; children: ReactNode }) {
  return <div className={`alert ${kind}`} role="status">{children}</div>;
}

const OUTCOME_TONE: Record<string, string> = {
  CONFIRMED: "ok", REFUSED: "danger", DEVICE_ERROR: "danger", DIAL_FAILED: "danger",
  ANYTIME: "warn", UNCONFIRMED: "warn", CONSULT_REQUESTED: "warn", WRONG_PERSON: "warn", NO_RESPONSE: "warn",
};

export function OutcomeBadge({ outcome }: { outcome?: string | null }) {
  if (!outcome) return <span className="badge live">통화중</span>;
  return <span className={`badge ${OUTCOME_TONE[outcome] ?? "muted"}`}>{OUTCOME[outcome] ?? outcome}</span>;
}

export function DateCell({ value, confirmed }: { value?: string | null; confirmed?: boolean }) {
  if (!value) return <span className="unknown">미확인</span>;
  return <span className={`date ${confirmed ? "confirmed" : ""}`}>{value}</span>;
}

export function MockBanner() {
  return (
    <div className="mock-banner" role="note">
      시험 조직(MOCK) — 실제 전화를 걸지 않으며 모든 통화 결과는 시험 데이터입니다.
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Page({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="page">
      <header className="page-head">
        <h1>{title}</h1>
        {actions && <div className="page-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
