"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type FaqRow = {
  id: number;
  question: string;
  answer: string;
  sort_order: number;
  is_active: boolean;
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        AI 客服屬進階功能,請至
        <a href="/console/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

export default function FaqPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<FaqRow | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const faqs = useQuery({
    queryKey: ["faq-admin"],
    queryFn: () => fetchJson<FaqRow[]>("/ai/faq"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["faq-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/ai/faq", input.body)
        : fetchJson(`/ai/faq/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => fetchJson(`/ai/faq/${id}`, { method: "DELETE" }),
    onSuccess: () => { invalidate(); setMessage({ kind: "ok", text: "已刪除。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (faqs.error instanceof ApiError && faqs.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">AI 客服 FAQ</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    saveMut.mutate({
      id: editing?.id ?? null,
      body: {
        question: String(form.get("question") || "").trim(),
        answer: String(form.get("answer") || "").trim(),
        sort_order: Number(form.get("sort_order") || 0),
        ...(editing ? { is_active: form.get("is_active") === "on" } : {}),
      },
    });
  }

  return (
    <div className="mx-auto max-w-4xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">AI 客服 FAQ</h1>
        <div className="flex items-center gap-3 text-sm">
          <a href="/ui/faq" className="text-muted hover:text-ink">AI 測試問答/未回答分析 → 舊版</a>
          <button
            onClick={() => { setEditing(null); setMessage(null); document.getElementById("faq-form")?.scrollIntoView(); }}
            className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep"
          >
            新增 FAQ
          </button>
        </div>
      </header>
      <p className="mt-2 text-sm text-muted">
        FAQ 是 AI 回答顧客的知識庫;顧客在 LINE 發問時,AI 會依這裡的內容作答。
      </p>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <ul className="mt-4 grid gap-2">
        {faqs.isLoading && <li className="text-sm text-muted">載入中…</li>}
        {faqs.data?.length === 0 && (
          <li className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-muted">
            尚無 FAQ。加入常見問題後,AI 客服才能替你回答。
          </li>
        )}
        {faqs.data?.map((f) => (
          <li key={f.id} className="rounded-xl border border-line bg-surface p-4 text-sm">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="font-semibold">
                  {f.question}
                  {!f.is_active && (
                    <span className="ml-2 rounded-full bg-line px-2 py-0.5 text-xs text-muted">停用</span>
                  )}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-muted">{f.answer}</p>
              </div>
              <div className="flex shrink-0 gap-1.5">
                <button onClick={() => { setEditing(f); setMessage(null); document.getElementById("faq-form")?.scrollIntoView(); }}
                  className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                  編輯
                </button>
                <button
                  onClick={() => { if (window.confirm("刪除這則 FAQ?")) deleteMut.mutate(f.id); }}
                  className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">
                  刪除
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      <section id="faq-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? "編輯 FAQ" : "新增 FAQ"}</h2>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm" onSubmit={submit}>
          <label className="grid gap-1">
            問題 *
            <input name="question" required defaultValue={editing?.question ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            答案 *
            <textarea name="answer" required rows={3} defaultValue={editing?.answer ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1 sm:max-w-[160px]">
            排序(小在前)
            <input name="sort_order" type="number" defaultValue={editing?.sort_order ?? 0}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {editing && (
            <label className="flex items-center gap-2">
              <input name="is_active" type="checkbox" defaultChecked={editing.is_active} />
              啟用中
            </label>
          )}
          <div className="flex gap-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立 FAQ"}
            </button>
            {editing && (
              <button type="button" onClick={() => setEditing(null)}
                className="rounded-lg border border-line px-4 py-2">
                取消編輯
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}
