"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type QuestionRow = {
  id: number;
  label: string;
  field_type: string;
  is_required: boolean;
  options: string[];
  sort_order: number;
};

type TemplateRow = {
  id: number;
  name: string;
  intro: string | null;
  consent_text: string;
  service_id: number | null;
  require_signature: boolean;
  is_active: boolean;
  version: number;
  questions: QuestionRow[];
};

type ServiceRow = { id: number; name: string };

const FIELD_TYPE_LABELS: Record<string, string> = {
  text: "單行文字",
  textarea: "多行文字",
  number: "數字",
  date: "日期",
  select: "下拉選單",
  checkbox: "勾選",
};

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        顧客表單／同意書屬進階功能,請至
        <a href="/console/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

function AddQuestionForm({
  templateId,
  onError,
}: {
  templateId: number;
  onError: (text: string) => void;
}) {
  const qc = useQueryClient();
  const [fieldType, setFieldType] = useState("text");

  const add = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson(`/api/v1/client-forms/${templateId}/questions`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["client-forms"] }),
    onError: (e) => onError(errText(e)),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    add.mutate({
      label: String(form.get("label") ?? ""),
      field_type: fieldType,
      required: form.get("required") === "on",
      options: String(form.get("options") ?? ""),
    });
    event.currentTarget.reset();
    setFieldType("text");
  }

  return (
    <form className="mt-3 flex flex-wrap items-end gap-2 text-sm" onSubmit={submit}>
      <label>
        問題
        <input name="label" required maxLength={255}
          className="mt-1 block w-56 rounded-lg border border-line bg-surface px-3 py-1.5" />
      </label>
      <label>
        類型
        <select value={fieldType} onChange={(e) => setFieldType(e.target.value)}
          className="mt-1 block rounded-lg border border-line bg-surface px-3 py-1.5">
          {Object.entries(FIELD_TYPE_LABELS).map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        </select>
      </label>
      {fieldType === "select" && (
        <label>
          選項(每行一個)
          <textarea name="options" rows={2}
            className="mt-1 block w-48 rounded-lg border border-line bg-surface px-3 py-1.5" />
        </label>
      )}
      <label className="flex items-center gap-1 pb-1.5">
        <input name="required" type="checkbox" /> 必填
      </label>
      <button disabled={add.isPending}
        className="rounded-lg bg-brand px-3 py-1.5 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
        加入題目
      </button>
    </form>
  );
}

export default function ClientFormsPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const templates = useQuery({
    queryKey: ["client-forms"],
    queryFn: () => fetchJson<TemplateRow[]>("/api/v1/client-forms"),
    retry: false,
  });
  const services = useQuery({
    queryKey: ["services-for-forms"],
    queryFn: () => fetchJson<ServiceRow[]>("/booking/services/"),
    retry: false,
  });

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/client-forms", body),
    onSuccess: () => {
      setMsg({ kind: "ok", text: "表單已建立,請加入題目後啟用。" });
      qc.invalidateQueries({ queryKey: ["client-forms"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const setActive = useMutation({
    mutationFn: (input: { id: number; active: boolean }) =>
      postJson(`/api/v1/client-forms/${input.id}/active`, { active: input.active }),
    onSuccess: () => {
      setMsg(null);
      qc.invalidateQueries({ queryKey: ["client-forms"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  if (templates.error instanceof ApiError && templates.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">諮詢表／同意書</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const serviceId = String(form.get("service_id") ?? "").trim();
    create.mutate({
      name: String(form.get("name") ?? ""),
      intro: String(form.get("intro") ?? ""),
      consent_text: String(form.get("consent_text") ?? ""),
      service_id: serviceId ? Number(serviceId) : null,
      require_signature: form.get("require_signature") === "on",
    });
    event.currentTarget.reset();
  }

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">諮詢表／同意書</h1>
      <p className="mt-1 text-sm text-muted">
        建立表單範本並綁定服務;預約後系統自動發送填寫連結,顧客簽名同意後存檔供稽核。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      <form className="mt-6 grid gap-3 rounded-xl border border-line bg-surface p-4" onSubmit={submit}>
        <h2 className="font-semibold">新增表單範本</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">
            名稱
            <input name="name" required maxLength={128}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            綁定服務(選填,綁定後僅該服務預約發送)
            <select name="service_id"
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2">
              <option value="">不綁定(所有預約)</option>
              {services.data?.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="text-sm">
          填寫說明(選填)
          <textarea name="intro" maxLength={4000} rows={2}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
        </label>
        <label className="text-sm">
          同意聲明(10～4,000 字,顧客須勾選同意)
          <textarea name="consent_text" required minLength={10} maxLength={4000} rows={3}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input name="require_signature" type="checkbox" defaultChecked /> 需要手寫簽名
        </label>
        <div>
          <button disabled={create.isPending}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            建立表單
          </button>
        </div>
      </form>

      <div className="mt-6 grid gap-4">
        {templates.isLoading && <p className="text-sm text-muted">載入中…</p>}
        {templates.data?.length === 0 && (
          <p className="rounded-xl border border-line bg-surface p-6 text-sm text-muted">尚無表單範本。</p>
        )}
        {templates.data?.map((t) => (
          <div key={t.id} className="rounded-xl border border-line bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="font-semibold">
                  {t.name}
                  <span className={`ml-2 rounded-full px-2 py-0.5 text-xs ${t.is_active ? "bg-ok-soft text-ok" : "bg-line/40 text-muted"}`}>
                    {t.is_active ? "啟用中" : "未啟用"}
                  </span>
                </h3>
                <p className="mt-0.5 text-xs text-muted">
                  v{t.version}
                  {t.require_signature ? "・需簽名" : ""}
                  {t.service_id ? `・綁定服務 #${t.service_id}` : "・所有預約"}
                </p>
              </div>
              <button
                className={`rounded-lg px-3 py-1.5 text-sm font-semibold ${t.is_active ? "border border-line hover:bg-line/20" : "bg-brand text-white hover:bg-brand-deep"}`}
                onClick={() => setActive.mutate({ id: t.id, active: !t.is_active })}
              >
                {t.is_active ? "停用" : "啟用"}
              </button>
            </div>
            {t.questions.length > 0 ? (
              <ol className="mt-3 grid gap-1 text-sm">
                {t.questions.map((q, i) => (
                  <li key={q.id} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-muted">{i + 1}.</span>
                    <span>{q.label}</span>
                    <span className="text-xs text-muted">
                      {FIELD_TYPE_LABELS[q.field_type] ?? q.field_type}
                      {q.is_required ? "・必填" : ""}
                      {q.options.length > 0 ? `・${q.options.join(" / ")}` : ""}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-3 text-sm text-muted">尚無題目;至少加入一題後才能啟用。</p>
            )}
            <AddQuestionForm templateId={t.id}
              onError={(text) => setMsg({ kind: "error", text })} />
          </div>
        ))}
      </div>
    </div>
  );
}
