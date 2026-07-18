"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type CouponRow = {
  id: number;
  code: string;
  name: string;
  discount_type: string;
  discount_value: number;
  min_spend_cents: number;
  max_redemptions: number | null;
  redeemed_count: number;
  active_from: string | null;
  active_until: string | null;
  is_active: boolean;
};

type RedemptionRow = {
  id: number;
  line_user_id: string;
  customer_id: number | null;
  reservation_id: number | null;
  order_id: number | null;
  redeemed_at: string;
};

const TYPE_LABELS: Record<string, string> = {
  percent: "折扣 %",
  amount: "折抵金額",
  gift: "贈品",
  upsell: "加購",
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function discountText(c: CouponRow): string {
  if (c.discount_type === "percent") return `${c.discount_value}% off`;
  if (c.discount_type === "amount") return `折 NT$${Math.floor(c.discount_value / 100)}`;
  return TYPE_LABELS[c.discount_type] ?? c.discount_type;
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        優惠券屬進階功能,請至
        <a href="/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

function Redemptions({ couponId }: { couponId: number }) {
  const redemptions = useQuery({
    queryKey: ["coupon-redemptions", couponId],
    queryFn: () => fetchJson<RedemptionRow[]>(`/booking/coupons/${couponId}/redemptions`),
    retry: false,
  });
  return (
    <div className="border-t border-line/60 bg-bg/40 px-4 py-3 text-sm">
      <h3 className="font-semibold">兌換紀錄</h3>
      {redemptions.isLoading && <p className="mt-1 text-muted">載入中…</p>}
      {redemptions.data?.length === 0 && <p className="mt-1 text-muted">尚無兌換。</p>}
      <ul className="mt-2 grid gap-1">
        {redemptions.data?.map((r) => (
          <li key={r.id} className="rounded-lg border border-line bg-surface px-3 py-1.5">
            {r.redeemed_at.slice(0, 16).replace("T", " ")}
            {r.customer_id !== null && ` · 顧客 #${r.customer_id}`}
            {r.reservation_id !== null && ` · 預約 #${r.reservation_id}`}
            {r.order_id !== null && ` · 訂單 #${r.order_id}`}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function CouponsPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<CouponRow | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const coupons = useQuery({
    queryKey: ["coupons-admin"],
    queryFn: () => fetchJson<CouponRow[]>("/booking/coupons/"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["coupons-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/booking/coupons/", input.body)
        : fetchJson(`/booking/coupons/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (coupons.error instanceof ApiError && coupons.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">優惠券</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const maxRed = String(form.get("max_redemptions") ?? "").trim();
    const until = String(form.get("active_until") ?? "").trim();
    if (editing) {
      saveMut.mutate({
        id: editing.id,
        body: {
          name: String(form.get("name") || "").trim(),
          ...(maxRed === "" ? {} : { max_redemptions: Number(maxRed) }),
          ...(until === "" ? {} : { active_until: until }),
          is_active: form.get("is_active") === "on",
        },
      });
      return;
    }
    const dtype = String(form.get("discount_type") || "percent");
    const rawValue = Number(form.get("discount_value") || 0);
    const from = String(form.get("active_from") ?? "").trim();
    saveMut.mutate({
      id: null,
      body: {
        code: String(form.get("code") || "").trim().toUpperCase(),
        name: String(form.get("name") || "").trim(),
        discount_type: dtype,
        // amount 以 NT$ 輸入 → cents;percent/其他直接存值
        discount_value: dtype === "amount" ? Math.round(rawValue * 100) : rawValue,
        min_spend_cents: Math.round(Number(form.get("min_spend_twd") || 0) * 100),
        ...(maxRed === "" ? {} : { max_redemptions: Number(maxRed) }),
        ...(from === "" ? {} : { active_from: from }),
        ...(until === "" ? {} : { active_until: until }),
      },
    });
  }

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">優惠券</h1>
        <button
          onClick={() => { setEditing(null); setMessage(null); document.getElementById("coupon-form")?.scrollIntoView(); }}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep"
        >
          新增優惠券
        </button>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">代碼</th>
              <th className="px-4 py-2.5 font-medium">名稱</th>
              <th className="px-4 py-2.5 font-medium">優惠</th>
              <th className="px-4 py-2.5 font-medium">已兌換</th>
              <th className="px-4 py-2.5 font-medium">效期</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {coupons.isLoading && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {coupons.data?.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">尚無優惠券。</td></tr>
            )}
            {coupons.data?.map((c) => (
              <>
                <tr key={c.id} className="border-b border-line/60">
                  <td className="px-4 py-2.5 font-mono font-medium">{c.code}</td>
                  <td className="px-4 py-2.5">{c.name}</td>
                  <td className="px-4 py-2.5">{discountText(c)}</td>
                  <td className="px-4 py-2.5">
                    {c.redeemed_count}{c.max_redemptions !== null && ` / ${c.max_redemptions}`}
                  </td>
                  <td className="px-4 py-2.5 text-xs">
                    {c.active_from ? c.active_from.slice(0, 10) : "即日"} → {c.active_until ? c.active_until.slice(0, 10) : "無期限"}
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded-full px-2 py-0.5 text-xs ${
                      c.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
                    }`}>{c.is_active ? "生效中" : "停用"}</span>
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex justify-end gap-1.5">
                      <button onClick={() => setExpanded(expanded === c.id ? null : c.id)}
                        className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                        {expanded === c.id ? "收合" : "兌換紀錄"}
                      </button>
                      <button onClick={() => { setEditing(c); setMessage(null); }}
                        className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                        編輯
                      </button>
                    </div>
                  </td>
                </tr>
                {expanded === c.id && (
                  <tr key={`${c.id}-detail`}>
                    <td colSpan={7} className="p-0"><Redemptions couponId={c.id} /></td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>

      <section id="coupon-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? `編輯:${editing.code}` : "新增優惠券"}</h2>
        <p className="mt-1 text-xs text-muted">
          {editing
            ? "代碼/優惠內容建立後不可改(避免已流通券變質);可改名稱、上限、效期迄與啟停。"
            : "代碼建立後不可修改。"}
        </p>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm sm:grid-cols-2" onSubmit={submit}>
          {!editing && (
            <label className="grid gap-1">
              代碼 *
              <input name="code" required maxLength={64} placeholder="WELCOME100"
                className="rounded-lg border border-line px-3 py-2 font-mono uppercase" />
            </label>
          )}
          <label className="grid gap-1">
            名稱 *
            <input name="name" required maxLength={128} defaultValue={editing?.name ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {!editing && (
            <>
              <label className="grid gap-1">
                優惠類型
                <select name="discount_type" defaultValue="percent"
                  className="rounded-lg border border-line px-3 py-2">
                  <option value="percent">折扣 %(值=百分比)</option>
                  <option value="amount">折抵金額(值=NT$)</option>
                  <option value="gift">贈品</option>
                  <option value="upsell">加購</option>
                </select>
              </label>
              <label className="grid gap-1">
                優惠值
                <input name="discount_value" type="number" min={0} defaultValue={10}
                  className="rounded-lg border border-line px-3 py-2" />
              </label>
              <label className="grid gap-1">
                低消(NT$)
                <input name="min_spend_twd" type="number" min={0} defaultValue={0}
                  className="rounded-lg border border-line px-3 py-2" />
              </label>
              <label className="grid gap-1">
                生效起(選填)
                <input name="active_from" type="datetime-local"
                  className="rounded-lg border border-line px-3 py-2" />
              </label>
            </>
          )}
          <label className="grid gap-1">
            兌換上限(留空=不限)
            <input name="max_redemptions" type="number" min={1}
              defaultValue={editing?.max_redemptions ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            生效迄(選填)
            <input name="active_until" type="datetime-local"
              defaultValue={editing?.active_until ? editing.active_until.slice(0, 16) : ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {editing && (
            <label className="flex items-center gap-2 sm:col-span-2">
              <input name="is_active" type="checkbox" defaultChecked={editing.is_active} />
              生效中
            </label>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立優惠券"}
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
