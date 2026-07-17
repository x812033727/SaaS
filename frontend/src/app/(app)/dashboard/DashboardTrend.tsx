"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/lib/client-api";
import TrendChart from "@/components/charts/TrendChart";

type TrendPoint = { period: string; bookings: number; revenue_cents: number };

/** Dashboard 趨勢圖(R6-D4):近 12 個月的預約量 + 營收,復用 reports 的 TrendChart。 */
export default function DashboardTrend() {
  const trend = useQuery({
    queryKey: ["dashboard-trend"],
    queryFn: () => fetchJson<TrendPoint[]>("/booking/analytics/trend?period=month&periods=12"),
  });

  return (
    <section className="mt-6 rounded-xl border border-line bg-surface p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-muted">近 12 個月趨勢</h2>
        <a href="/reports" className="text-sm text-brand hover:underline">完整報表 →</a>
      </div>
      {trend.isLoading && <p className="mt-3 text-sm text-muted">載入中…</p>}
      {trend.data && trend.data.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <TrendChart
            points={trend.data.map((p) => ({
              label: p.period,
              bookings: p.bookings,
              revenueTwd: Math.floor(p.revenue_cents / 100),
            }))}
          />
        </div>
      )}
      {trend.data && trend.data.length === 0 && (
        <p className="mt-3 text-sm text-muted">尚無足夠資料繪製趨勢。</p>
      )}
    </section>
  );
}
