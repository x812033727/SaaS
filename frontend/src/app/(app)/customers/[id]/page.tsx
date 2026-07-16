"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { use, useState } from "react";

import { ApiError, fetchJson, fetchList, patchJson, postJson } from "@/lib/client-api";
import type { CustomerRow } from "../page";

type CustomerDetail = CustomerRow & {
  note: string | null;
  blacklist_reason: string | null;
};

type ReservationRow = {
  id: number;
  status: string;
  party_size: number;
  slot_start: string;
  service_name: string | null;
  staff_name: string | null;
};

type PointTx = { id: number; delta: number; reason: string; created_at: string };
type Tag = { id: number; name: string };

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

export default function CustomerDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const qc = useQueryClient();
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const customer = useQuery({
    queryKey: ["customer", id],
    queryFn: () => fetchJson<CustomerDetail>(`/booking/customers/${id}`),
  });
  const history = useQuery({
    queryKey: ["customer-reservations", id],
    queryFn: () => fetchList<ReservationRow>(`/api/v1/customers/${id}/reservations?limit=50`),
  });
  const points = useQuery({
    queryKey: ["customer-points", id],
    queryFn: () => fetchJson<PointTx[]>(`/booking/customers/${id}/points`),
  });
  const tags = useQuery({
    queryKey: ["customer-tags", id],
    queryFn: () => fetchJson<Tag[]>(`/booking/customers/${id}/tags`),
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["customer", id] });
    qc.invalidateQueries({ queryKey: ["customer-points", id] });
  };

  const updateMut = useMutation({
    mutationFn: (body: { phone?: string | null; note?: string | null }) =>
      patchJson(`/booking/customers/${id}`, body),
    onSuccess: () => { refresh(); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });
  const blacklistMut = useMutation({
    mutationFn: (body: { blacklisted: boolean; reason?: string }) =>
      postJson(`/booking/customers/${id}/blacklist`, body),
    onSuccess: () => { refresh(); setMessage({ kind: "ok", text: "黑名單狀態已更新。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });
  const pointsMut = useMutation({
    mutationFn: (body: { delta: number; reason: string }) =>
      postJson(`/booking/customers/${id}/points`, body),
    onSuccess: () => { refresh(); setMessage({ kind: "ok", text: "點數已調整。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (customer.isLoading) return <p className="text-muted">載入中…</p>;
  if (!customer.data) return <p className="text-muted">找不到顧客。</p>;
  const c = customer.data;

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">
            {c.display_name ?? `顧客 #${c.id}`}
            {c.blacklisted && (
              <span className="ml-2 rounded-full bg-danger-soft px-2 py-0.5 text-sm text-danger">黑名單</span>
            )}
          </h1>
          <p className="mt-1 text-sm text-muted">
            LINE:{c.line_user_id.slice(0, 12)}… · 預約 {c.booking_count} 次 · {c.tier}
          </p>
        </div>
        <a href={`/ui/customers`} className="text-sm text-muted hover:text-ink">進階功能 → 舊版頁面</a>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>
          {message.text}
        </p>
      )}

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-line bg-surface p-5">
          <h2 className="font-semibold">基本資料</h2>
          <form
            className="mt-3 grid gap-3 text-sm"
            onSubmit={(e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              updateMut.mutate({
                phone: String(form.get("phone") || "") || null,
                note: String(form.get("note") || "") || null,
              });
            }}
          >
            <label className="grid gap-1">
              電話
              <input name="phone" defaultValue={c.phone ?? ""} maxLength={32}
                className="rounded-lg border border-line px-2 py-2" />
            </label>
            <label className="grid gap-1">
              備註
              <textarea name="note" defaultValue={c.note ?? ""} rows={3} maxLength={2048}
                className="rounded-lg border border-line px-2 py-2" />
            </label>
            <button disabled={updateMut.isPending}
              className="justify-self-start rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              儲存
            </button>
          </form>
          <div className="mt-4 border-t border-line pt-4 text-sm">
            {c.blacklisted ? (
              <div className="flex items-center justify-between gap-3">
                <p className="text-danger">已列黑名單{c.blacklist_reason ? `:${c.blacklist_reason}` : ""}</p>
                <button onClick={() => blacklistMut.mutate({ blacklisted: false })}
                  className="rounded-lg border border-line px-3 py-1.5 hover:bg-ok-soft">解除黑名單</button>
              </div>
            ) : (
              <button
                onClick={() => {
                  const reason = window.prompt("列入黑名單原因(可留空):") ?? undefined;
                  if (reason !== undefined || window.confirm("確定列入黑名單?")) {
                    blacklistMut.mutate({ blacklisted: true, reason: reason || undefined });
                  }
                }}
                className="rounded-lg border border-line px-3 py-1.5 text-danger hover:bg-danger-soft">
                列入黑名單
              </button>
            )}
          </div>
        </section>

        <section className="rounded-xl border border-line bg-surface p-5">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">點數</h2>
            <p className="text-2xl font-semibold text-brand">{c.points_balance}</p>
          </div>
          <form
            className="mt-3 flex gap-2 text-sm"
            onSubmit={(e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              const delta = Number(form.get("delta"));
              if (!delta) return;
              pointsMut.mutate({ delta, reason: String(form.get("reason") || "manual") });
              e.currentTarget.reset();
            }}
          >
            <input name="delta" type="number" placeholder="±點數" required
              className="w-24 rounded-lg border border-line px-2 py-1.5" />
            <input name="reason" placeholder="原因" maxLength={64}
              className="flex-1 rounded-lg border border-line px-2 py-1.5" />
            <button className="rounded-lg border border-line px-3 py-1.5 hover:bg-brand-soft">調整</button>
          </form>
          <ul className="mt-3 max-h-44 space-y-1 overflow-y-auto text-sm">
            {(points.data ?? []).map((tx) => (
              <li key={tx.id} className="flex justify-between border-b border-line/60 py-1">
                <span className="text-muted">{tx.created_at.slice(0, 10)} {tx.reason}</span>
                <span className={tx.delta >= 0 ? "text-ok" : "text-danger"}>
                  {tx.delta >= 0 ? `+${tx.delta}` : tx.delta}
                </span>
              </li>
            ))}
          </ul>
          {(tags.data?.length ?? 0) > 0 && (
            <div className="mt-4 border-t border-line pt-3">
              <p className="text-sm font-medium">標籤</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {tags.data!.map((t) => (
                  <span key={t.id} className="rounded-full bg-gold-soft px-2.5 py-0.5 text-xs text-ink">
                    {t.name}
                  </span>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>

      <section className="mt-4 rounded-xl border border-line bg-surface p-5">
        <h2 className="font-semibold">預約歷史</h2>
        {history.data?.rows.length === 0 ? (
          <p className="mt-3 text-sm text-muted">沒有預約紀錄。</p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <th className="py-2 pr-4 font-medium">時間</th>
                  <th className="py-2 pr-4 font-medium">服務</th>
                  <th className="py-2 pr-4 font-medium">員工</th>
                  <th className="py-2 pr-4 font-medium">人數</th>
                  <th className="py-2 font-medium">狀態</th>
                </tr>
              </thead>
              <tbody>
                {(history.data?.rows ?? []).map((r) => (
                  <tr key={r.id} className="border-b border-line/60">
                    <td className="py-2 pr-4">{r.slot_start.slice(0, 10)} {r.slot_start.slice(11, 16)}</td>
                    <td className="py-2 pr-4">{r.service_name ?? "—"}</td>
                    <td className="py-2 pr-4">{r.staff_name ?? "—"}</td>
                    <td className="py-2 pr-4">{r.party_size}</td>
                    <td className="py-2">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${
                        r.status === "confirmed" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
                      }`}>
                        {r.status === "confirmed" ? "已確認" : "已取消"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
