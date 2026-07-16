"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type SlotRow = {
  id: number;
  slot_start: string;
  slot_end: string | null;
  max_capacity: number;
  walkin_reserved: number;
  booked_count: number;
  is_active: boolean;
  online_available: number;
};

const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function todayIso(offsetDays = 0): string {
  const d = new Date(Date.now() + offsetDays * 86400_000);
  return d.toISOString().slice(0, 10);
}

export default function SlotsPage() {
  const qc = useQueryClient();
  const [dateFrom, setDateFrom] = useState(todayIso());
  const [dateTo, setDateTo] = useState(todayIso(14));
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const slots = useQuery({
    queryKey: ["slots-admin", dateFrom, dateTo],
    queryFn: () =>
      fetchJson<SlotRow[]>(`/booking/slots/?date_from=${dateFrom}&date_to=${dateTo}`),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["slots-admin"] });

  const toggleMut = useMutation({
    mutationFn: (slot: SlotRow) =>
      fetchJson(`/booking/slots/${slot.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !slot.is_active }),
      }),
    onSuccess: invalidate,
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const bulkMut = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson<{ created: number; skipped: number; total: number }>(
        "/booking/slots/bulk", body,
      ),
    onSuccess: (r) => {
      invalidate();
      setMessage({ kind: "ok", text: `批次完成:新建 ${r.created} 筆、略過 ${r.skipped} 筆(已存在)。` });
    },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  function submitBulk(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const weekdays = WEEKDAY_LABELS
      .map((_, i) => (form.get(`wd${i}`) === "on" ? i : null))
      .filter((v): v is number => v !== null);
    bulkMut.mutate({
      date_start: form.get("date_start"),
      date_end: form.get("date_end"),
      time_start: form.get("time_start"),
      time_end: form.get("time_end"),
      interval_minutes: Number(form.get("interval_minutes") || 60),
      max_capacity: Number(form.get("max_capacity") || 1),
      walkin_reserved: Number(form.get("walkin_reserved") || 0),
      ...(weekdays.length > 0 && weekdays.length < 7 ? { weekdays } : {}),
    });
  }

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">時段管理</h1>
        <a href="/ui/booking" className="text-sm text-muted hover:text-ink">單筆建立/刪除 → 舊版</a>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <section className="mt-4 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">批次建立時段</h2>
        <p className="mt-1 text-xs text-muted">日期區間 × 每日營業時間 × 間隔一鍵展開;既存同時刻時段自動略過。</p>
        <form className="mt-4 grid gap-3 text-sm sm:grid-cols-3" onSubmit={submitBulk}>
          <label className="grid gap-1">起日 *
            <input name="date_start" type="date" required defaultValue={todayIso(1)}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">迄日 *
            <input name="date_end" type="date" required defaultValue={todayIso(14)}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">間隔(分)
            <input name="interval_minutes" type="number" min={5} defaultValue={60}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">每日開始 *
            <input name="time_start" type="time" required defaultValue="10:00"
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">每日結束 *
            <input name="time_end" type="time" required defaultValue="20:00"
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">容量
            <input name="max_capacity" type="number" min={0} defaultValue={4}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <fieldset className="sm:col-span-3">
            <legend className="text-xs text-muted">星期(全不勾=每天)</legend>
            <div className="mt-1 flex flex-wrap gap-3">
              {WEEKDAY_LABELS.map((label, i) => (
                <label key={i} className="flex items-center gap-1">
                  <input name={`wd${i}`} type="checkbox" />週{label}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="sm:col-span-3">
            <button disabled={bulkMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {bulkMut.isPending ? "建立中…" : "批次建立"}
            </button>
          </div>
        </form>
      </section>

      <section className="mt-5">
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="grid gap-1">列表起日
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
              className="rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="grid gap-1">列表迄日
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
              className="rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          {slots.data && <p className="ml-auto self-center text-muted">共 {slots.data.length} 筆</p>}
        </div>
        <div className="mt-3 overflow-x-auto rounded-xl border border-line bg-surface">
          <table className="w-full min-w-[560px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2.5 font-medium">時間</th>
                <th className="px-4 py-2.5 font-medium">容量</th>
                <th className="px-4 py-2.5 font-medium">已訂</th>
                <th className="px-4 py-2.5 font-medium">可約</th>
                <th className="px-4 py-2.5 font-medium">狀態</th>
                <th className="px-4 py-2.5 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {slots.isLoading && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
              )}
              {slots.data?.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">此區間沒有時段。</td></tr>
              )}
              {slots.data?.map((s) => (
                <tr key={s.id} className="border-b border-line/60">
                  <td className="px-4 py-2.5 font-medium">
                    {s.slot_start.slice(0, 10)} {s.slot_start.slice(11, 16)}
                  </td>
                  <td className="px-4 py-2.5">{s.max_capacity}{s.walkin_reserved > 0 && (
                    <span className="text-xs text-muted">(留現場 {s.walkin_reserved})</span>
                  )}</td>
                  <td className="px-4 py-2.5">{s.booked_count}</td>
                  <td className="px-4 py-2.5">{s.online_available}</td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded-full px-2 py-0.5 text-xs ${
                      s.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
                    }`}>{s.is_active ? "開放" : "停用"}</span>
                  </td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => toggleMut.mutate(s)}
                      className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                      {s.is_active ? "停用" : "開放"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
