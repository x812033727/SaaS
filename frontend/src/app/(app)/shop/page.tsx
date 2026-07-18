"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type ProductRow = {
  id: number;
  name: string;
  description: string | null;
  price_cents: number;
  currency: string;
  stock: number | null;
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
        商品銷售屬進階功能,請至
        <a href="/console/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

export default function ShopPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<ProductRow | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const products = useQuery({
    queryKey: ["products-admin"],
    queryFn: () => fetchJson<ProductRow[]>("/booking/products/"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["products-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/booking/products/", input.body)
        : fetchJson(`/booking/products/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (products.error instanceof ApiError && products.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">商品</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const stock = String(form.get("stock") ?? "").trim();
    saveMut.mutate({
      id: editing?.id ?? null,
      body: {
        name: String(form.get("name") || "").trim(),
        price_cents: Math.round(Number(form.get("price_twd") || 0) * 100),
        description: String(form.get("description") || "").trim() || null,
        ...(stock === "" ? { stock: null } : { stock: Number(stock) }),
        ...(editing ? { is_active: form.get("is_active") === "on" } : {}),
      },
    });
  }

  return (
    <div className="mx-auto max-w-4xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">商品</h1>
        <button
          onClick={() => { setEditing(null); setMessage(null); document.getElementById("prod-form")?.scrollIntoView(); }}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep"
        >
          新增商品
        </button>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">名稱</th>
              <th className="px-4 py-2.5 font-medium">價格</th>
              <th className="px-4 py-2.5 font-medium">庫存</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {products.isLoading && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {products.data?.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">尚無商品。</td></tr>
            )}
            {products.data?.map((p) => (
              <tr key={p.id} className="border-b border-line/60">
                <td className="px-4 py-2.5">
                  <span className="font-medium">{p.name}</span>
                  {p.description && <span className="ml-2 text-xs text-muted">{p.description}</span>}
                </td>
                <td className="px-4 py-2.5">NT${Math.floor(p.price_cents / 100).toLocaleString()}</td>
                <td className="px-4 py-2.5">{p.stock === null ? "不限" : p.stock}</td>
                <td className="px-4 py-2.5">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${
                    p.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
                  }`}>{p.is_active ? "上架" : "下架"}</span>
                </td>
                <td className="px-4 py-2.5">
                  <button onClick={() => { setEditing(p); setMessage(null); }}
                    className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                    編輯
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section id="prod-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? `編輯:${editing.name}` : "新增商品"}</h2>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm sm:grid-cols-2" onSubmit={submit}>
          <label className="grid gap-1 sm:col-span-2">
            名稱 *
            <input name="name" required maxLength={128} defaultValue={editing?.name ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            價格(NT$)
            <input name="price_twd" type="number" min={0}
              defaultValue={editing ? Math.floor(editing.price_cents / 100) : 0}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            庫存(留空=不限)
            <input name="stock" type="number" min={0}
              defaultValue={editing?.stock ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1 sm:col-span-2">
            說明
            <input name="description" maxLength={2048} defaultValue={editing?.description ?? ""}
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
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立商品"}
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
