"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson } from "@/lib/client-api";

type ProductRow = {
  id: number;
  name: string;
  description: string | null;
  price_cents: number;
  currency: string;
  stock: number | null;
  is_active: boolean;
};

type OrderRow = {
  id: number;
  status: string;
  total_cents: number;
  refund_status: string | null;
  refunded_cents: number;
  created_at: string | null;
};

const ORDER_STATUS_LABEL: Record<string, string> = {
  pending: "待付款",
  paid: "已付款",
  cancelled: "已取消",
};

const REFUND_LABEL: Record<string, string> = {
  refunded: "已退款",
  partially_refunded: "部分退款",
  processing: "退款處理中",
  manual_required: "需人工對帳",
  failed: "退款失敗",
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

  const orders = useQuery({
    queryKey: ["shop-orders"],
    queryFn: () => fetchJson<OrderRow[]>("/api/v1/orders"),
    retry: false,
  });
  const invalidateOrders = () => qc.invalidateQueries({ queryKey: ["shop-orders"] });

  // R12-C1:/ui 退役後訂單退款只剩此入口。amount 留空=全額餘額。
  const refundMut = useMutation({
    mutationFn: (input: { id: number; amount_twd: number | null }) =>
      postJson(`/api/v1/orders/${input.id}/refund`,
        input.amount_twd ? { amount_twd: input.amount_twd } : {}),
    onSuccess: () => { invalidateOrders(); setMessage({ kind: "ok", text: "退款已送出。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });
  const manualRefundMut = useMutation({
    mutationFn: (input: { id: number; note: string; amount_twd: number | null }) =>
      postJson(`/api/v1/orders/${input.id}/refund/manual`, {
        note: input.note,
        ...(input.amount_twd ? { amount_twd: input.amount_twd } : {}),
      }),
    onSuccess: () => { invalidateOrders(); setMessage({ kind: "ok", text: "已對帳為人工退款。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  function askRefund(o: OrderRow) {
    const raw = window.prompt(
      `退款金額(NT$,留空=全額 NT$${Math.floor((o.total_cents - o.refunded_cents) / 100)})`, "");
    if (raw === null) return;
    const amount = raw.trim() === "" ? null : Number(raw);
    if (amount !== null && (!Number.isInteger(amount) || amount < 1)) {
      setMessage({ kind: "error", text: "金額須為正整數(或留空=全額)。" });
      return;
    }
    if (!window.confirm(`確認對訂單 #${o.id} 退款${amount ? ` NT$${amount}` : "(全額)"}?`)) return;
    refundMut.mutate({ id: o.id, amount_twd: amount });
  }

  function askManualRefund(o: OrderRow) {
    const note = window.prompt("已在金流後台退款的對帳備註(必填):", "");
    if (note === null) return;
    if (note.trim().length < 2) {
      setMessage({ kind: "error", text: "備註至少 2 個字。" });
      return;
    }
    const raw = window.prompt("對帳金額(NT$,留空=全額):", "");
    if (raw === null) return;
    const amount = raw.trim() === "" ? null : Number(raw);
    if (amount !== null && (!Number.isInteger(amount) || amount < 1)) {
      setMessage({ kind: "error", text: "金額須為正整數(或留空=全額)。" });
      return;
    }
    manualRefundMut.mutate({ id: o.id, note: note.trim(), amount_twd: amount });
  }

  const delProductMut = useMutation({
    mutationFn: (id: number) => delJson(`/booking/products/${id}`),
    onSuccess: () => { invalidate(); setMessage({ kind: "ok", text: "已刪除。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

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
                  <div className="flex gap-1.5">
                    <button onClick={() => { setEditing(p); setMessage(null); }}
                      className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                      編輯
                    </button>
                    <button onClick={() =>
                        window.confirm(`確定刪除「${p.name}」?此動作無法復原。`)
                        && delProductMut.mutate(p.id)}
                      disabled={delProductMut.isPending}
                      className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">
                      刪除
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section className="mt-8">
        <h2 className="text-lg font-semibold">訂單</h2>
        <div className="mt-3 overflow-x-auto rounded-xl border border-line bg-surface">
          <table className="w-full min-w-[560px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2.5 font-medium">#</th>
                <th className="px-4 py-2.5 font-medium">建立時間</th>
                <th className="px-4 py-2.5 font-medium">金額</th>
                <th className="px-4 py-2.5 font-medium">狀態</th>
                <th className="px-4 py-2.5 font-medium">退款</th>
                <th className="px-4 py-2.5 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {orders.isLoading && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
              )}
              {orders.data?.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">尚無訂單。</td></tr>
              )}
              {orders.data?.map((o) => (
                <tr key={o.id} className="border-b border-line/60">
                  <td className="px-4 py-2.5">#{o.id}</td>
                  <td className="px-4 py-2.5 text-muted">
                    {o.created_at ? new Date(o.created_at).toLocaleString("zh-TW") : "—"}
                  </td>
                  <td className="px-4 py-2.5">NT${Math.floor(o.total_cents / 100).toLocaleString()}</td>
                  <td className="px-4 py-2.5">{ORDER_STATUS_LABEL[o.status] ?? o.status}</td>
                  <td className="px-4 py-2.5">
                    {o.refund_status
                      ? `${REFUND_LABEL[o.refund_status] ?? o.refund_status}(NT$${Math.floor(o.refunded_cents / 100)})`
                      : "—"}
                  </td>
                  <td className="px-4 py-2.5">
                    {o.status === "paid" && o.refund_status !== "refunded" && (
                      <div className="flex gap-2">
                        <button onClick={() => askRefund(o)}
                          disabled={refundMut.isPending}
                          className="rounded-md border border-line px-2 py-1 text-xs hover:bg-danger-soft">
                          退款
                        </button>
                        <button onClick={() => askManualRefund(o)}
                          disabled={manualRefundMut.isPending}
                          className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft"
                          title="已在金流後台退款後,在系統對帳(不會重複退刷)">
                          人工對帳
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

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
