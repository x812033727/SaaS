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

type WalletCredit = {
  customer_package_id: number;
  package_name: string;
  service_id: number;
  service_name: string;
  remaining: number;
  expires_at: string | null;
};
type PackageLedgerRow = {
  id: number;
  customer_package_id: number;
  kind: string;
  delta: number;
  created_at: string | null;
};
type CustomerPackages = { wallet: WalletCredit[]; ledger: PackageLedgerRow[] };
type PackageDef = { id: number; name: string; price_cents: number; is_active: boolean };

const LEDGER_KIND: Record<string, string> = {
  issue: "發行",
  redeem: "扣抵",
  refund: "退回",
  adjust: "調整",
  cancel: "作廢沖銷",
};

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
  // R12-C2:顧客套票操作(/ui 退役後只剩此入口);feature 未開 → 403 → 隱藏區塊
  const pkgs = useQuery({
    queryKey: ["customer-packages", id],
    queryFn: () => fetchJson<CustomerPackages>(`/api/v1/customers/${id}/packages`),
    retry: false,
  });
  const packageDefs = useQuery({
    queryKey: ["package-defs"],
    queryFn: () => fetchJson<PackageDef[]>("/api/v1/packages"),
    retry: false,
    enabled: !(pkgs.error instanceof ApiError && pkgs.error.status === 403),
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
  const refreshPkgs = () => qc.invalidateQueries({ queryKey: ["customer-packages", id] });
  const issuePkgMut = useMutation({
    mutationFn: (package_id: number) =>
      postJson(`/api/v1/customers/${id}/packages`, {
        package_id,
        issuance_key: `console-${id}-${package_id}-${Date.now()}`,
      }),
    onSuccess: () => { refreshPkgs(); setMessage({ kind: "ok", text: "套票已發行。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });
  const cancelPkgMut = useMutation({
    mutationFn: (customer_package_id: number) =>
      postJson(`/api/v1/customers/${id}/packages/${customer_package_id}/cancel`, {}),
    onSuccess: () => { refreshPkgs(); setMessage({ kind: "ok", text: "套票已作廢。" }); },
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
            {c.line_user_id ? `LINE:${c.line_user_id.slice(0, 12)}…` : "無 LINE(walk-in/網路預約)"}
            {" "}· 預約 {c.booking_count} 次 · {c.tier}
          </p>
        </div>
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

      {!(pkgs.error instanceof ApiError && pkgs.error.status === 403) && (
        <section className="mt-4 rounded-xl border border-line bg-surface p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="font-semibold">服務套票</h2>
            <div className="flex items-center gap-2 text-sm">
              <select id="issue-pkg" className="rounded-lg border border-line px-2 py-1.5">
                <option value="">選擇套票…</option>
                {(packageDefs.data ?? []).filter((p) => p.is_active).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}(NT${Math.floor(p.price_cents / 100)})
                  </option>
                ))}
              </select>
              <button
                onClick={() => {
                  const sel = document.getElementById("issue-pkg") as HTMLSelectElement | null;
                  const pid = sel?.value ? Number(sel.value) : null;
                  if (!pid) { setMessage({ kind: "error", text: "請先選擇套票。" }); return; }
                  if (window.confirm("確認對此顧客發行套票?")) issuePkgMut.mutate(pid);
                }}
                disabled={issuePkgMut.isPending}
                className="rounded-lg bg-brand px-3 py-1.5 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
                發行套票
              </button>
            </div>
          </div>
          {pkgs.data?.wallet.length === 0 ? (
            <p className="mt-3 text-sm text-muted">目前沒有可用套票。</p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[480px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-muted">
                    <th className="py-2 pr-4 font-medium">套票</th>
                    <th className="py-2 pr-4 font-medium">服務</th>
                    <th className="py-2 pr-4 font-medium">剩餘次數</th>
                    <th className="py-2 pr-4 font-medium">到期</th>
                    <th className="py-2 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {(pkgs.data?.wallet ?? []).map((w) => (
                    <tr key={`${w.customer_package_id}-${w.service_id}`} className="border-b border-line/60">
                      <td className="py-2 pr-4 font-medium">{w.package_name}</td>
                      <td className="py-2 pr-4">{w.service_name}</td>
                      <td className="py-2 pr-4">{w.remaining}</td>
                      <td className="py-2 pr-4 text-muted">{w.expires_at ? w.expires_at.slice(0, 10) : "—"}</td>
                      <td className="py-2">
                        <button
                          onClick={() =>
                            window.confirm("作廢此套票?未使用次數將以帳本沖銷(不處理金流退款)。")
                            && cancelPkgMut.mutate(w.customer_package_id)}
                          disabled={cancelPkgMut.isPending}
                          className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">
                          作廢
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(pkgs.data?.ledger.length ?? 0) > 0 && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-muted">次數帳本({pkgs.data?.ledger.length})</summary>
              <ul className="mt-2 grid gap-1">
                {(pkgs.data?.ledger ?? []).map((l) => (
                  <li key={l.id} className="text-muted">
                    {l.created_at ? l.created_at.slice(0, 16).replace("T", " ") : "—"} ·{" "}
                    {LEDGER_KIND[l.kind] ?? l.kind} {l.delta > 0 ? `+${l.delta}` : l.delta}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}
    </div>
  );
}
