"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type ServiceRow = {
  id: number;
  name: string;
  category_id: number | null;
  duration_minutes: number;
  price_cents: number;
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
        服務目錄屬進階功能,請至
        <a href="/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

export default function ServicesPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<ServiceRow | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const services = useQuery({
    queryKey: ["services-admin"],
    queryFn: () => fetchJson<ServiceRow[]>("/booking/services/"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["services-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/booking/services/", input.body)
        : fetchJson(`/booking/services/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (services.error instanceof ApiError && services.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">服務項目</h1>
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
        name: String(form.get("name") || "").trim(),
        duration_minutes: Number(form.get("duration_minutes") || 60),
        price_cents: Math.round(Number(form.get("price_twd") || 0) * 100),
        ...(editing ? { is_active: form.get("is_active") === "on" } : {}),
      },
    });
  }

  return (
    <div className="mx-auto max-w-4xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">服務項目</h1>
        <div className="flex items-center gap-3 text-sm">
          <a href="/ui/services" className="text-muted hover:text-ink">分類/指定員工 → 舊版</a>
          <button
            onClick={() => { setEditing(null); setMessage(null); document.getElementById("svc-form")?.scrollIntoView(); }}
            className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep"
          >
            新增服務
          </button>
        </div>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[520px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">名稱</th>
              <th className="px-4 py-2.5 font-medium">時長</th>
              <th className="px-4 py-2.5 font-medium">價格</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {services.isLoading && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {services.data?.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">尚無服務項目。</td></tr>
            )}
            {services.data?.map((s) => (
              <tr key={s.id} className="border-b border-line/60">
                <td className="px-4 py-2.5 font-medium">{s.name}</td>
                <td className="px-4 py-2.5">{s.duration_minutes} 分</td>
                <td className="px-4 py-2.5">NT${Math.floor(s.price_cents / 100).toLocaleString()}</td>
                <td className="px-4 py-2.5">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${
                    s.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
                  }`}>{s.is_active ? "上架" : "停用"}</span>
                </td>
                <td className="px-4 py-2.5">
                  <button onClick={() => { setEditing(s); setMessage(null); }}
                    className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                    編輯
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section id="svc-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? `編輯:${editing.name}` : "新增服務"}</h2>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm sm:grid-cols-2" onSubmit={submit}>
          <label className="grid gap-1 sm:col-span-2">
            名稱 *
            <input name="name" required maxLength={128} defaultValue={editing?.name ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            時長(分鐘)
            <input name="duration_minutes" type="number" min={0} defaultValue={editing?.duration_minutes ?? 60}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            價格(NT$)
            <input name="price_twd" type="number" min={0}
              defaultValue={editing ? Math.floor(editing.price_cents / 100) : 0}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {editing && (
            <label className="flex items-center gap-2 sm:col-span-2">
              <input name="is_active" type="checkbox" defaultChecked={editing.is_active} />
              上架中
            </label>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立服務"}
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
