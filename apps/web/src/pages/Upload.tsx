import { useState } from "react";
import { api, ApiError, ERROR_TEXT, Me } from "../api";
import { go } from "../App";
import { ErrorBox, Field, Notice, Page, useAsync } from "../ui";

const FIELDS: [string, string][] = [
  ["external_no", "간병인 번호"], ["name", "이름"], ["phone", "연락처"], ["date", "근무확정일"],
];

export default function UploadPage({ me }: { me: Me }) {
  const history = useAsync(() => api.get<any[]>("/api/v1/imports"));
  const [up, setUp] = useState<any>(null);
  const [mapping, setMapping] = useState<Record<string, number | null>>({});
  const [semantics, setSemantics] = useState<string>("");
  const [val, setVal] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const canUpload = me.can.upload_dial;

  async function onFile(f: File | undefined) {
    if (!f) return;
    setErr(null); setVal(null); setUp(null); setBusy(true);
    try {
      const r = await api.upload("/api/v1/imports", f);
      setUp(r);
      setMapping(r.suggested_mapping);
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  async function validate() {
    setErr(null); setBusy(true);
    try {
      setVal(await api.post(`/api/v1/imports/${up.import_id}/validate`, { mapping, date_semantics: semantics || null }));
    } catch (e) { setErr(e as ApiError); setVal(null); } finally { setBusy(false); }
  }

  async function commit() {
    setErr(null); setBusy(true);
    try {
      await api.post(`/api/v1/imports/${up.import_id}/commit`, { validation_version: val.validation_version });
      go("/targets", { batch: up.import_id });
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  }

  return (
    <Page title="명부 업로드">
      {!canUpload && <Notice kind="warn">이 계정은 명부를 올릴 수 없습니다. 검수와 조회만 가능합니다.</Notice>}
      <div className="panel">
        <p className="lead">
          간병사 명부 엑셀(.xlsx)을 올리면 간병인 번호·이름·연락처·근무확정일 열을 찾아 검사합니다.
          매크로 문서(xlsm)·암호화 파일은 받지 않으며, 원본 행 순서와 값은 결과 파일에 그대로 남습니다.
        </p>
        <label className={`dropzone ${busy ? "busy" : ""}`}>
          <input type="file" accept=".xlsx" disabled={!canUpload || busy} onChange={(e) => onFile(e.target.files?.[0])} />
          <span>{up ? `${up.file_name} — ${up.row_count}행` : "xlsx 파일 선택 (최대 10MB, 1만 행)"}</span>
        </label>
        <ErrorBox error={err} />
      </div>

      {up && (
        <div className="panel">
          <h2>열 매핑</h2>
          {up.duplicate_of && (
            <Notice kind="warn">같은 파일을 {up.duplicate_of.created_at}에 이미 올렸습니다. 중복 캠페인이 되지 않게 확인하세요.</Notice>
          )}
          <div className="grid4">
            {FIELDS.map(([k, label]) => (
              <Field key={k} label={label}>
                <select value={mapping[k] ?? ""} onChange={(e) => { setVal(null); setMapping({ ...mapping, [k]: e.target.value === "" ? null : Number(e.target.value) }); }}>
                  <option value="">선택 안 함</option>
                  {up.detected_columns.map((c: string, i: number) => <option key={i} value={i}>{c}</option>)}
                </select>
              </Field>
            ))}
          </div>
          <fieldset className="semantics">
            <legend>‘근무확정일’ 열은 무엇을 뜻합니까?</legend>
            <label>
              <input type="radio" name="sem" value="NEXT_AVAILABLE" checked={semantics === "NEXT_AVAILABLE"} onChange={(e) => { setVal(null); setSemantics(e.target.value); }} />
              <span><strong>다음 근무 가능일</strong> — 통화에서 간병사가 재확인해 동의한 날짜로 이 열을 갱신합니다.</span>
            </label>
            <label>
              <input type="radio" name="sem" value="ASSIGNMENT_CONFIRMED" checked={semantics === "ASSIGNMENT_CONFIRMED"} onChange={(e) => { setVal(null); setSemantics(e.target.value); }} />
              <span><strong>실제 배정 확정일</strong> — 이 열은 그대로 두고, 통화 결과는 별도 ‘다음근무가능일’ 열에 적습니다.</span>
            </label>
          </fieldset>
          <h3>미리보기</h3>
          <table className="table compact">
            <thead><tr><th>행</th><th>간병인 번호</th><th>이름</th><th>연락처</th><th>근무확정일</th></tr></thead>
            <tbody>
              {up.preview.map((r: any) => (
                <tr key={r.row_no}><td className="num">{r.row_no}</td><td className="num">{r.external_no}</td><td>{r.name}</td><td className="num">{r.phone}</td><td className="num">{r.date ?? ""}</td></tr>
              ))}
            </tbody>
          </table>
          <div className="actions">
            <button className="btn primary" disabled={busy || !semantics} onClick={validate}>검사하기</button>
            {!semantics && <span className="muted">근무확정일의 의미를 먼저 선택하세요.</span>}
          </div>
        </div>
      )}

      {val && (
        <div className="panel">
          <h2>검사 결과</h2>
          <dl className="stats">
            <div><dt>데이터 행</dt><dd>{val.data_rows}</dd></div>
            <div><dt>발신 가능</dt><dd>{val.dialable_count}</dd></div>
            <div><dt>오류 행</dt><dd className={val.error_count ? "warn" : ""}>{val.error_count}</dd></div>
            <div><dt>연락거부 제외</dt><dd>{val.blocked_count}</dd></div>
            <div><dt>연락처 공유·중복</dt><dd>{val.shared_phone_count + val.duplicate_count}</dd></div>
          </dl>
          {val.errors.length > 0 ? (
            <>
              <p className="muted">오류·주의 행은 발신 대기열에 들어가지 않습니다. 원본 파일에서 고쳐 다시 올리거나, 정상 행만 먼저 저장할 수 있습니다.</p>
              <table className="table compact">
                <thead><tr><th>행</th><th>문제</th><th>간병인 번호</th><th>이름</th><th>연락처</th></tr></thead>
                <tbody>
                  {val.errors.map((e: any, i: number) => (
                    <tr key={i}><td className="num">{e["행 번호"]}</td><td>{ERROR_TEXT[e["오류코드"]] ?? e["설명"]}</td><td className="num">{e["간병인 번호"]}</td><td>{e["이름"]}</td><td className="num">{e["연락처(마스킹)"]}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : <Notice kind="ok">모든 행이 정상입니다.</Notice>}
          <div className="actions">
            <button className="btn primary" disabled={busy || val.valid_count === 0} onClick={commit}>
              정상 {val.valid_count}행 저장하고 대상 선택으로
            </button>
          </div>
        </div>
      )}

      <div className="panel">
        <h2>최근 업로드</h2>
        {(history.data ?? []).length === 0 ? <p className="muted">아직 올린 명부가 없습니다.</p> : (
          <table className="table compact">
            <thead><tr><th>파일</th><th>올린 시각(KST)</th><th>행</th><th>상태</th><th /></tr></thead>
            <tbody>
              {history.data!.map((b) => (
                <tr key={b.id}>
                  <td>{b.file_name}</td><td className="num">{b.created_at}</td><td className="num">{b.row_count}</td>
                  <td>{b.status === "COMMITTED" ? "저장됨" : b.status === "VALIDATED" ? "검사됨" : "업로드됨"}</td>
                  <td>{b.status === "COMMITTED" && <a href={`#/targets?batch=${b.id}`}>대상 보기</a>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Page>
  );
}
