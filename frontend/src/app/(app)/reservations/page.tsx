"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ApiError, fetchJson, fetchList, postJson } from "@/lib/client-api";

type ReservationRow = {
  id: number;
  status: string;
  party_size: number;
  attended: boolean | null;
  slot_start: string;
  slot_id: number;
  customer_id: number | null;
  customer_name: string | null;
  customer_phone: string | null;
  staff_name: string | null;
  service_name: string | null;
  deposit_status: string | null;
};

type SlotRow = { id: number; slot_start: string; online_available: number; is_active: boolean };
type WaitlistRow = {
  id: number;
  slot_id: number;
  status: string;
  party_size: number;
  display_name: string | null;
  created_at: string | null;
  slot_start: string | null;
};
type ServiceRow = { id: number; name: string; is_active: boolean };
type StaffRow = { id: number; name: string; is_active: boolean };

const PAGE_SIZE = 25;

function fmt(iso: string): string {
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function ReservationsInner() {
  const params = useSearchParams();
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [dateFrom, setDateFrom] = useState(params.get("date_from") ?? "");
  const [dateTo, setDateTo] = useState(params.get("date_to") ?? "");
  const [page, setPage] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [rescheduleTarget, setRescheduleTarget] = useState<ReservationRow | null>(null);
  const [actionError, setActionError] = useState("");

  const query = new URLSearchParams();
  if (status) query.set("status", status);
  if (dateFrom) query.set("date_from", dateFrom);
  if (dateTo) query.set("date_to", dateTo);
  query.set("limit", String(PAGE_SIZE));
  query.set("offset", String(page * PAGE_SIZE));

  const { data, isLoading } = useQuery({
    queryKey: ["reservations", query.toString()],
    queryFn: () => fetchList<ReservationRow>(`/api/v1/reservations?${query}`),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["reservations"] });

  const cancelMut = useMutation({
    mutationFn: (id: number) => postJson(`/booking/reservations/${id}/cancel`, {}),
    onSuccess: invalidate,
    onError: (e) => setActionError(errText(e)),
  });
  const attendMut = useMutation({
    mutationFn: ({ id, attended }: { id: number; attended: boolean }) =>
      postJson(`/booking/reservations/${id}/attendance`, { attended }),
    onSuccess: invalidate,
    onError: (e) => setActionError(errText(e)),
  });

  // R12-C1:/ui 退役後定金退款只剩此入口(取消後的已付定金)。
  const depositRefundMut = useMutation({
    mutationFn: (input: { id: number; amount_twd: number | null }) =>
      postJson(`/api/v1/reservations/${input.id}/deposit-refund`,
        input.amount_twd ? { amount_twd: input.amount_twd } : {}),
    onSuccess: () => { invalidate(); setActionError(""); },
    onError: (e) => setActionError(errText(e)),
  });
  const depositManualMut = useMutation({
    mutationFn: (input: { id: number; note: string }) =>
      postJson(`/api/v1/reservations/${input.id}/deposit-refund/manual`, { note: input.note }),
    onSuccess: () => { invalidate(); setActionError(""); },
    onError: (e) => setActionError(errText(e)),
  });

  function askDepositRefund(r: ReservationRow) {
    const raw = window.prompt("退還定金金額(NT$,留空=全額):", "");
    if (raw === null) return;
    const amount = raw.trim() === "" ? null : Number(raw);
    if (amount !== null && (!Number.isInteger(amount) || amount < 1)) {
      setActionError("金額須為正整數(或留空=全額)。");
      return;
    }
    if (!window.confirm(`確認退還預約 #${r.id} 的定金${amount ? ` NT$${amount}` : "(全額)"}?`)) return;
    depositRefundMut.mutate({ id: r.id, amount_twd: amount });
  }

  function askDepositManual(r: ReservationRow) {
    const note = window.prompt("已在金流後台退款的對帳備註(必填):", "");
    if (note === null) return;
    if (note.trim().length < 2) { setActionError("備註至少 2 個字。"); return; }
    depositManualMut.mutate({ id: r.id, note: note.trim() });
  }

  const waitlist = useQuery({
    queryKey: ["waitlist"],
    queryFn: () => fetchJson<WaitlistRow[]>("/api/v1/waitlist"),
  });
  const waitlistCancelMut = useMutation({
    mutationFn: (id: number) => postJson(`/api/v1/waitlist/${id}/cancel`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["waitlist"] }),
    onError: (e) => setActionError(errText(e)),
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">預約管理</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCreate(true)}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep"
          >
            建立預約
          </button>
        </div>
      </header>

      <div className="mt-4 flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 text-sm">
        <label className="grid gap-1">
          狀態
          <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(0); }}
            className="rounded-lg border border-line px-2 py-1.5">
            <option value="">全部</option>
            <option value="confirmed">已確認</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
        <label className="grid gap-1">
          起(含)
          <input type="date" value={dateFrom} onChange={(e) => { setDateFrom(e.target.value); setPage(0); }}
            className="rounded-lg border border-line px-2 py-1.5" />
        </label>
        <label className="grid gap-1">
          迄(不含)
          <input type="date" value={dateTo} onChange={(e) => { setDateTo(e.target.value); setPage(0); }}
            className="rounded-lg border border-line px-2 py-1.5" />
        </label>
        {data && <p className="ml-auto text-muted">共 {data.total} 筆</p>}
      </div>

      {actionError && (
        <p className="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-sm text-danger">{actionError}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">#</th>
              <th className="px-4 py-2.5 font-medium">時間</th>
              <th className="px-4 py-2.5 font-medium">顧客</th>
              <th className="px-4 py-2.5 font-medium">服務／員工</th>
              <th className="px-4 py-2.5 font-medium">人數</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {data?.rows.length === 0 && (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-muted">沒有符合的預約。</td></tr>
            )}
            {data?.rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60 align-top">
                <td className="px-4 py-2.5 text-muted">{r.id}</td>
                <td className="px-4 py-2.5 font-medium">{fmt(r.slot_start)}</td>
                <td className="px-4 py-2.5">
                  {r.customer_name ?? "—"}
                  {r.customer_phone && <div className="text-xs text-muted">{r.customer_phone}</div>}
                </td>
                <td className="px-4 py-2.5">
                  {r.service_name ?? "—"}
                  {r.staff_name && <div className="text-xs text-muted">{r.staff_name}</div>}
                </td>
                <td className="px-4 py-2.5">{r.party_size}</td>
                <td className="px-4 py-2.5">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${
                    r.status === "confirmed" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
                  }`}>
                    {r.status === "confirmed" ? "已確認" : r.status === "cancelled" ? "已取消" : r.status}
                  </span>
                  {r.deposit_status === "pending" && (
                    <span className="ml-1 rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn">待付定金</span>
                  )}
                  {r.deposit_status === "paid" && (
                    <span className="ml-1 rounded-full bg-ok-soft px-2 py-0.5 text-xs text-ok">已付定金</span>
                  )}
                  {r.deposit_status === "refunded" && (
                    <span className="ml-1 rounded-full bg-line px-2 py-0.5 text-xs text-muted">定金已退</span>
                  )}
                  {r.attended === true && <span className="ml-1 text-xs text-ok">已到場</span>}
                  {r.attended === false && <span className="ml-1 text-xs text-danger">未到</span>}
                </td>
                <td className="px-4 py-2.5">
                  {r.status === "confirmed" && (
                    <div className="flex flex-wrap gap-1.5">
                      <button onClick={() => attendMut.mutate({ id: r.id, attended: true })}
                        className="rounded-md border border-line px-2 py-1 text-xs hover:bg-ok-soft">到場</button>
                      <button onClick={() => attendMut.mutate({ id: r.id, attended: false })}
                        className="rounded-md border border-line px-2 py-1 text-xs hover:bg-warn-soft">未到</button>
                      <button onClick={() => setRescheduleTarget(r)}
                        className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">改期</button>
                      <button
                        onClick={() => window.confirm(`確定取消預約 #${r.id}?`) && cancelMut.mutate(r.id)}
                        className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">取消</button>
                    </div>
                  )}
                  {r.status === "cancelled" && r.deposit_status === "paid" && (
                    <div className="flex flex-wrap gap-1.5">
                      <button onClick={() => askDepositRefund(r)}
                        disabled={depositRefundMut.isPending}
                        className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">
                        退定金
                      </button>
                      <button onClick={() => askDepositManual(r)}
                        disabled={depositManualMut.isPending}
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

      <div className="mt-3 flex items-center justify-end gap-2 text-sm">
        <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}
          className="rounded-md border border-line px-3 py-1 disabled:opacity-40">上一頁</button>
        <span className="text-muted">{page + 1} / {totalPages}</span>
        <button disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)}
          className="rounded-md border border-line px-3 py-1 disabled:opacity-40">下一頁</button>
      </div>

      <section className="mt-8">
        <h2 className="text-lg font-semibold">候補名單</h2>
        <div className="mt-3 overflow-x-auto rounded-xl border border-line bg-surface">
          <table className="w-full min-w-[560px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2.5 font-medium">時段</th>
                <th className="px-4 py-2.5 font-medium">顧客</th>
                <th className="px-4 py-2.5 font-medium">人數</th>
                <th className="px-4 py-2.5 font-medium">狀態</th>
                <th className="px-4 py-2.5 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {waitlist.isLoading && (
                <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
              )}
              {waitlist.data?.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">目前沒有候補。</td></tr>
              )}
              {waitlist.data?.map((w) => (
                <tr key={w.id} className="border-b border-line/60">
                  <td className="px-4 py-2.5 font-medium">{w.slot_start ? fmt(w.slot_start) : "—"}</td>
                  <td className="px-4 py-2.5">{w.display_name ?? "—"}</td>
                  <td className="px-4 py-2.5">{w.party_size}</td>
                  <td className="px-4 py-2.5">
                    {w.status === "waiting" ? "等待中"
                      : w.status === "notified" ? "已通知"
                      : w.status === "cancelled" ? "已取消"
                      : w.status === "booked" ? "已成單" : w.status}
                  </td>
                  <td className="px-4 py-2.5">
                    {(w.status === "waiting" || w.status === "notified") && (
                      <button
                        onClick={() =>
                          window.confirm("取消這筆候補?") && waitlistCancelMut.mutate(w.id)}
                        disabled={waitlistCancelMut.isPending}
                        className="rounded-md border border-line px-2 py-1 text-xs text-danger hover:bg-danger-soft">
                        取消候補
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {showCreate && (
        <CreateDialog
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); invalidate(); }}
        />
      )}
      {rescheduleTarget && (
        <RescheduleDialog
          reservation={rescheduleTarget}
          onClose={() => setRescheduleTarget(null)}
          onDone={() => { setRescheduleTarget(null); invalidate(); }}
        />
      )}
    </div>
  );
}

function useSlotOptions(daysAhead = 30) {
  const today = new Date();
  const from = today.toISOString().slice(0, 10);
  const to = new Date(today.getTime() + daysAhead * 86400_000).toISOString().slice(0, 10);
  return useQuery({
    queryKey: ["slots", from, to],
    queryFn: () =>
      fetchJson<SlotRow[]>(`/booking/slots/?date_from=${from}&date_to=${to}&active_only=true`),
  });
}

function Dialog({ title, onClose, children }: {
  title: string; onClose: () => void; children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-line bg-surface p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} className="text-muted hover:text-ink">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function CreateDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const slots = useSlotOptions();
  const services = useQuery({
    queryKey: ["services"],
    queryFn: () => fetchJson<ServiceRow[]>("/booking/services/"),
  });
  const staff = useQuery({
    queryKey: ["staff"],
    queryFn: () => fetchJson<StaffRow[]>("/booking/staff/"),
  });
  const [error, setError] = useState("");
  const createMut = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/booking/reservations/", body),
    onSuccess: onCreated,
    onError: (e) => setError(errText(e)),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const body: Record<string, unknown> = {
      slot_id: Number(form.get("slot_id")),
      party_size: Number(form.get("party_size") || 1),
    };
    const display = String(form.get("display_name") || "").trim();
    if (display) body.display_name = display;
    if (form.get("service_id")) body.service_id = Number(form.get("service_id"));
    if (form.get("staff_id")) body.staff_id = Number(form.get("staff_id"));
    createMut.mutate(body);
  }

  return (
    <Dialog title="建立預約(店家代訂)" onClose={onClose}>
      <form className="mt-4 grid gap-3 text-sm" onSubmit={submit}>
        <label className="grid gap-1">
          時段 *
          <select name="slot_id" required className="rounded-lg border border-line px-2 py-2">
            <option value="">選擇時段</option>
            {(slots.data ?? [])
              .filter((s) => s.is_active && s.online_available > 0)
              .map((s) => (
                <option key={s.id} value={s.id}>
                  {fmt(s.slot_start)}(可約 {s.online_available})
                </option>
              ))}
          </select>
        </label>
        <label className="grid gap-1">
          顧客姓名
          <input name="display_name" maxLength={64} placeholder="現場/電話預約顧客"
            className="rounded-lg border border-line px-2 py-2" />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1">
            人數 *
            <input name="party_size" type="number" min={1} max={20} defaultValue={1} required
              className="rounded-lg border border-line px-2 py-2" />
          </label>
          <label className="grid gap-1">
            服務
            <select name="service_id" className="rounded-lg border border-line px-2 py-2">
              <option value="">不指定</option>
              {(services.data ?? []).filter((s) => s.is_active).map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="grid gap-1">
          員工
          <select name="staff_id" className="rounded-lg border border-line px-2 py-2">
            <option value="">不指定</option>
            {(staff.data ?? []).filter((s) => s.is_active).map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        {error && <p className="rounded-lg bg-danger-soft px-3 py-2 text-danger">{error}</p>}
        <button disabled={createMut.isPending}
          className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
          {createMut.isPending ? "建立中…" : "建立預約"}
        </button>
      </form>
    </Dialog>
  );
}

function RescheduleDialog({ reservation, onClose, onDone }: {
  reservation: ReservationRow; onClose: () => void; onDone: () => void;
}) {
  const slots = useSlotOptions();
  const [error, setError] = useState("");
  const mut = useMutation({
    mutationFn: (newSlotId: number) =>
      postJson(`/booking/reservations/${reservation.id}/reschedule`, { new_slot_id: newSlotId }),
    onSuccess: onDone,
    onError: (e) => setError(errText(e)),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    mut.mutate(Number(form.get("new_slot_id")));
  }

  return (
    <Dialog title={`改期預約 #${reservation.id}`} onClose={onClose}>
      <p className="mt-2 text-sm text-muted">目前時段:{fmt(reservation.slot_start)}</p>
      <form className="mt-4 grid gap-3 text-sm" onSubmit={submit}>
        <label className="grid gap-1">
          新時段 *
          <select name="new_slot_id" required className="rounded-lg border border-line px-2 py-2">
            <option value="">選擇時段</option>
            {(slots.data ?? [])
              .filter((s) => s.is_active && s.online_available > 0 && s.id !== reservation.slot_id)
              .map((s) => (
                <option key={s.id} value={s.id}>
                  {fmt(s.slot_start)}(可約 {s.online_available})
                </option>
              ))}
          </select>
        </label>
        {error && <p className="rounded-lg bg-danger-soft px-3 py-2 text-danger">{error}</p>}
        <button disabled={mut.isPending}
          className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
          {mut.isPending ? "改期中…" : "確認改期"}
        </button>
      </form>
    </Dialog>
  );
}

export default function ReservationsPage() {
  return (
    <Suspense fallback={<p className="text-muted">載入中…</p>}>
      <ReservationsInner />
    </Suspense>
  );
}
