"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import TrendChart from "@/components/charts/TrendChart";
import { ApiError, fetchJson } from "@/lib/client-api";

type TrendPoint = { period: string; bookings: number; revenue_cents: number };
type Revenue = { paid_orders: number; revenue_cents: number; avg_order_cents: number };
type Summary = {
  total: number; confirmed: number; cancelled: number; cancel_rate: number;
  total_covers: number; distinct_customers: number;
  attended: number; no_show: number; no_show_rate: number | null;
};
type Utilization = { hour: number; booked: number; capacity: number; utilization: number };
type TopCustomer = {
  id: number;
  display_name: string | null;
  line_user_id: string | null;
  booking_count: number;
  points_balance: number;
  tier: string;
};
type StaffPerf = {
  staff_name: string;
  reservation_count: number;
  revenue_cents: number;
  attended: number;
  no_show: number;
  attendance_rate: number | null;
};
type PopularService = { service_id: number | null; service_name: string; reservation_count: number };
type ReturnRate = { total_customers: number; repeat_customers: number; return_rate: number };
type LocationRow = { id: number; name: string };

export default function ReportsPage() {
  const [period, setPeriod] = useState<"week" | "month">("week");
  // 期間/分店篩選:date_to 補到當日終點使範圍含尾;空值=全期間/全店
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [locationId, setLocationId] = useState("");

  const rangeQs = [
    dateFrom ? `date_from=${dateFrom}T00:00:00` : "",
    dateTo ? `date_to=${dateTo}T23:59:59` : "",
  ].filter(Boolean).join("&");
  const advQs = [rangeQs, locationId ? `location_id=${locationId}` : ""]
    .filter(Boolean).join("&");
  const suffix = (qs: string) => (qs ? `?${qs}` : "");

  const trend = useQuery({
    queryKey: ["report-trend", period],
    queryFn: () => fetchJson<TrendPoint[]>(`/booking/analytics/trend?period=${period}&periods=12`),
  });
  const revenue = useQuery({
    queryKey: ["report-revenue", rangeQs],
    queryFn: () => fetchJson<Revenue>(`/booking/analytics/revenue${suffix(rangeQs)}`),
  });
  const summary = useQuery({
    queryKey: ["report-summary", rangeQs],
    queryFn: () => fetchJson<Summary>(`/booking/analytics/summary${suffix(rangeQs)}`),
  });
  const utilization = useQuery({
    queryKey: ["report-utilization", rangeQs],
    queryFn: () => fetchJson<Utilization[]>(`/booking/analytics/utilization${suffix(rangeQs)}`),
  });
  const customers = useQuery({
    queryKey: ["report-customers"],
    queryFn: () => fetchJson<TopCustomer[]>("/booking/analytics/customers?limit=10"),
  });
  const locations = useQuery({
    queryKey: ["report-locations"],
    queryFn: () => fetchJson<LocationRow[]>("/booking/locations/"),
    retry: false,
  });
  // 進階報表(ADVANCED_REPORTING 閘門;403 → 顯示升級提示)
  const staffPerf = useQuery({
    queryKey: ["report-staff-perf", advQs],
    queryFn: () => fetchJson<StaffPerf[]>(`/booking/analytics/report/staff-performance${suffix(advQs)}`),
    retry: false,
  });
  const popular = useQuery({
    queryKey: ["report-popular", advQs],
    queryFn: () => fetchJson<PopularService[]>(`/booking/analytics/report/popular-services${suffix(advQs)}`),
    retry: false,
  });
  const returnRate = useQuery({
    queryKey: ["report-return-rate", advQs],
    queryFn: () => fetchJson<ReturnRate>(`/booking/analytics/report/return-rate${suffix(advQs)}`),
    retry: false,
  });
  const advancedLocked =
    staffPerf.error instanceof ApiError && staffPerf.error.status === 403;

  const cards: [string, string, string][] = [
    ["累計實收", `NT$${Math.floor((revenue.data?.revenue_cents ?? 0) / 100).toLocaleString()}`,
      `${revenue.data?.paid_orders ?? 0} 張已付訂單`],
    ["客單價", `NT$${Math.floor((revenue.data?.avg_order_cents ?? 0) / 100).toLocaleString()}`, "已付訂單平均"],
    ["預約取消率", `${((summary.data?.cancel_rate ?? 0) * 100).toFixed(1)}%`,
      `${summary.data?.cancelled ?? 0} / ${summary.data?.total ?? 0} 筆`],
    ["爽約率", summary.data?.no_show_rate == null ? "—" : `${(summary.data.no_show_rate * 100).toFixed(1)}%`,
      summary.data?.no_show_rate == null ? "尚無到場標記" : `${summary.data.no_show} 筆未到`],
  ];

  const maxUtil = Math.max(0.01, ...(utilization.data ?? []).map((u) => u.utilization));

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">營運報表</h1>
        <div className="flex items-center gap-2 text-sm">
          <a href={`/console/api/proxy/booking/analytics/export.csv${suffix(rangeQs)}`}
            className="rounded-lg border border-line px-3 py-1 text-muted hover:text-ink">
            預約明細 CSV
          </a>
          {!advancedLocked && (
            <>
              <a href={`/console/api/proxy/booking/analytics/report.xlsx${suffix(advQs)}`}
                className="rounded-lg border border-line px-3 py-1 text-muted hover:text-ink">
                Excel
              </a>
              <a href={`/console/api/proxy/booking/analytics/report.pdf${suffix(advQs)}`}
                className="rounded-lg border border-line px-3 py-1 text-muted hover:text-ink">
                PDF
              </a>
            </>
          )}
          <div className="rounded-lg border border-line p-0.5">
            {(["week", "month"] as const).map((p) => (
              <button key={p} onClick={() => setPeriod(p)}
                className={`rounded-md px-3 py-1 ${period === p ? "bg-brand text-white" : "text-muted"}`}>
                {p === "week" ? "週" : "月"}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="mt-4 flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-3 text-sm">
        <label>起日
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
            className="mt-1 block rounded-lg border border-line bg-surface px-3 py-1.5" />
        </label>
        <label>迄日
          <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
            className="mt-1 block rounded-lg border border-line bg-surface px-3 py-1.5" />
        </label>
        {(locations.data?.length ?? 0) > 0 && (
          <label>分店(僅套用進階報表)
            <select value={locationId} onChange={(e) => setLocationId(e.target.value)}
              className="mt-1 block rounded-lg border border-line bg-surface px-3 py-1.5">
              <option value="">全部分店</option>
              {locations.data!.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          </label>
        )}
        {(dateFrom || dateTo || locationId) && (
          <button className="rounded-lg border border-line px-3 py-1.5 text-muted hover:text-ink"
            onClick={() => { setDateFrom(""); setDateTo(""); setLocationId(""); }}>
            清除篩選
          </button>
        )}
        <span className="text-xs text-muted">未選=全期間;趨勢圖固定近 12 期不受篩選影響。</span>
      </div>

      <section className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map(([label, value, hint]) => (
          <article key={label} className="rounded-xl border border-line bg-surface p-5 shadow-sm">
            <p className="text-sm text-muted">{label}</p>
            <p className="mt-2 text-2xl font-semibold text-brand">{value}</p>
            <p className="mt-1 text-xs text-muted">{hint}</p>
          </article>
        ))}
      </section>

      <section className="mt-5 rounded-xl border border-line bg-surface p-6">
        <div className="flex items-center gap-4">
          <h2 className="font-semibold">近 12 {period === "week" ? "週" : "月"}趨勢</h2>
          <span className="flex items-center gap-1 text-xs text-muted">
            <span className="inline-block h-2.5 w-2.5 rounded-sm bg-brand" />預約
          </span>
          <span className="flex items-center gap-1 text-xs text-muted">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold" />營收
          </span>
        </div>
        {trend.isLoading ? (
          <p className="mt-6 text-sm text-muted">載入中…</p>
        ) : (
          <div className="mt-4">
            <TrendChart
              points={(trend.data ?? []).map((p) => ({
                label: p.period.replace(/^\d{4}-/, ""),
                bookings: p.bookings,
                revenueTwd: Math.floor(p.revenue_cents / 100),
              }))}
            />
          </div>
        )}
      </section>

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-line bg-surface p-6">
          <h2 className="font-semibold">時段使用率(依小時)</h2>
          <div className="mt-4 space-y-2">
            {(utilization.data ?? []).length === 0 && (
              <p className="text-sm text-muted">尚無時段資料。</p>
            )}
            {(utilization.data ?? []).map((u) => (
              <div key={u.hour} className="flex items-center gap-2 text-xs">
                <span className="w-10 text-muted">{String(u.hour).padStart(2, "0")}:00</span>
                <div className="h-4 flex-1 rounded bg-brand-soft">
                  <div
                    className="h-4 rounded bg-brand"
                    style={{ width: `${(u.utilization / maxUtil) * 100}%` }}
                  />
                </div>
                <span className="w-14 text-right text-muted">
                  {(u.utilization * 100).toFixed(0)}%({u.booked}/{u.capacity})
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-xl border border-line bg-surface p-6">
          <h2 className="font-semibold">Top 10 顧客</h2>
          <table className="mt-3 w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="py-2 font-medium">顧客</th>
                <th className="py-2 font-medium">預約數</th>
                <th className="py-2 font-medium">等級</th>
              </tr>
            </thead>
            <tbody>
              {(customers.data ?? []).map((c) => (
                <tr key={c.id} className="border-b border-line/60">
                  <td className="py-2">{c.display_name ?? c.line_user_id ?? `顧客 #${c.id}`}</td>
                  <td className="py-2">{c.booking_count}</td>
                  <td className="py-2">{c.tier}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      {/* ── 進階報表(ADVANCED_REPORTING)── */}
      {advancedLocked ? (
        <section className="mt-5 rounded-xl border border-line bg-warn-soft p-6 text-sm">
          <p className="font-semibold text-warn">進階報表未啟用</p>
          <p className="mt-2 text-ink">
            員工產能/熱門服務/回訪率與 Excel/PDF 匯出屬進階功能,請至
            <a href="/console/plan" className="mx-1 text-brand underline">方案頁</a>升級或啟用。
          </p>
        </section>
      ) : (
        <div className="mt-5 grid gap-4 lg:grid-cols-3">
          <section className="rounded-xl border border-line bg-surface p-6 lg:col-span-2">
            <h2 className="font-semibold">員工產能</h2>
            <p className="mt-0.5 text-xs text-muted">推估營收=服務定價×筆數,非實收。</p>
            <table className="mt-3 w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <th className="py-2 font-medium">員工</th>
                  <th className="py-2 font-medium">預約</th>
                  <th className="py-2 font-medium">推估營收</th>
                  <th className="py-2 font-medium">到場/未到</th>
                  <th className="py-2 font-medium">到場率</th>
                </tr>
              </thead>
              <tbody>
                {(staffPerf.data ?? []).length === 0 && (
                  <tr><td colSpan={5} className="py-4 text-center text-muted">尚無資料。</td></tr>
                )}
                {(staffPerf.data ?? []).map((s) => (
                  <tr key={s.staff_name} className="border-b border-line/60">
                    <td className="py-2">{s.staff_name}</td>
                    <td className="py-2">{s.reservation_count}</td>
                    <td className="py-2">NT${Math.floor(s.revenue_cents / 100).toLocaleString()}</td>
                    <td className="py-2">{s.attended}/{s.no_show}</td>
                    <td className="py-2">
                      {s.attendance_rate == null ? "需標記" : `${(s.attendance_rate * 100).toFixed(0)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <div className="grid gap-4">
            <section className="rounded-xl border border-line bg-surface p-6">
              <h2 className="font-semibold">熱門服務</h2>
              <ul className="mt-3 grid gap-1 text-sm">
                {(popular.data ?? []).length === 0 && <li className="text-muted">尚無資料。</li>}
                {(popular.data ?? []).slice(0, 8).map((s) => (
                  <li key={`${s.service_id}-${s.service_name}`} className="flex justify-between">
                    <span>{s.service_name}</span>
                    <span className="text-muted">{s.reservation_count} 筆</span>
                  </li>
                ))}
              </ul>
            </section>
            <section className="rounded-xl border border-line bg-surface p-6">
              <h2 className="font-semibold">回訪率</h2>
              {returnRate.data && (
                <>
                  <p className="mt-2 text-2xl font-semibold text-brand">
                    {(returnRate.data.return_rate * 100).toFixed(1)}%
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {returnRate.data.repeat_customers} / {returnRate.data.total_customers} 位顧客回訪
                  </p>
                </>
              )}
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
