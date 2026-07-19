import { redirect } from "next/navigation";

import { apiFetch } from "@/lib/api";
import DashboardTrend from "./DashboardTrend";

type ReservationRow = {
  id: number;
  status: string;
  party_size: number;
  attended: boolean | null;
  slot_start: string;
  customer_name: string | null;
  customer_phone: string | null;
  staff_name: string | null;
  service_name: string | null;
  deposit_status: string | null;
};

type DashboardToday = {
  date: string;
  reservations: ReservationRow[];
  summary: { total: number } & Record<string, unknown>;
  revenue: { revenue_cents?: number; paid_orders?: number } & Record<string, unknown>;
  pending: {
    waitlist_waiting: number;
    deposits_pending: number;
    attendance_unmarked: number;
  };
};

function hhmm(iso: string): string {
  return iso.slice(11, 16);
}

const STATUS_BADGE: Record<string, string> = {
  confirmed: "bg-ok-soft text-ok",
  cancelled: "bg-danger-soft text-danger",
};

export default async function DashboardPage() {
  let data: DashboardToday;
  try {
    data = await apiFetch<DashboardToday>("/api/v1/dashboard/today");
  } catch (error) {
    if (error instanceof Error && error.message === "UNAUTHENTICATED") redirect("/login");
    throw error;
  }

  const revenueTwd = Math.floor((Number(data.revenue.revenue_cents) || 0) / 100);
  const cards: [string, string, string][] = [
    ["今日預約", String(data.summary.total ?? data.reservations.length), "筆"],
    ["今日實收", `NT$${revenueTwd.toLocaleString()}`, `${data.revenue.paid_orders ?? 0} 張訂單`],
    ["候補中", String(data.pending.waitlist_waiting), "位顧客"],
    ["待付定金", String(data.pending.deposits_pending), "筆預約"],
  ];

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">今日營運</h1>
          <p className="mt-1 text-sm text-muted">{data.date}</p>
        </div>
        {data.pending.attendance_unmarked > 0 && (
          <p className="rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn">
            {data.pending.attendance_unmarked} 筆已過時段尚未點名
          </p>
        )}
      </header>

      <section className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map(([label, value, hint]) => (
          <article key={label} className="rounded-xl border border-line bg-surface p-5 shadow-sm">
            <p className="text-sm text-muted">{label}</p>
            <p className="mt-2 text-2xl font-semibold text-brand">{value}</p>
            <p className="mt-1 text-xs text-muted">{hint}</p>
          </article>
        ))}
      </section>

      <DashboardTrend />

      <section className="mt-6 rounded-xl border border-line bg-surface p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">今日預約</h2>
          <a href="/console/reservations" className="text-sm text-brand hover:underline">
            前往預約管理 →
          </a>
        </div>
        {data.reservations.length === 0 ? (
          <p className="mt-4 text-sm text-muted">今天沒有預約。</p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <th className="py-2 pr-4 font-medium">時間</th>
                  <th className="py-2 pr-4 font-medium">顧客</th>
                  <th className="py-2 pr-4 font-medium">服務</th>
                  <th className="py-2 pr-4 font-medium">人數</th>
                  <th className="py-2 pr-4 font-medium">員工</th>
                  <th className="py-2 font-medium">狀態</th>
                </tr>
              </thead>
              <tbody>
                {data.reservations.map((r) => (
                  <tr key={r.id} className="border-b border-line/60">
                    <td className="py-2.5 pr-4 font-medium">{hhmm(r.slot_start)}</td>
                    <td className="py-2.5 pr-4">
                      {r.customer_name ?? "—"}
                      {r.customer_phone && (
                        <span className="ml-2 text-xs text-muted">{r.customer_phone}</span>
                      )}
                    </td>
                    <td className="py-2.5 pr-4">{r.service_name ?? "—"}</td>
                    <td className="py-2.5 pr-4">{r.party_size}</td>
                    <td className="py-2.5 pr-4">{r.staff_name ?? "—"}</td>
                    <td className="py-2.5">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${STATUS_BADGE[r.status] ?? "bg-brand-soft text-brand"}`}
                      >
                        {r.status === "confirmed" ? "已確認" : r.status === "cancelled" ? "已取消" : r.status}
                      </span>
                      {r.deposit_status === "pending" && (
                        <span className="ml-1 rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn">
                          待付定金
                        </span>
                      )}
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
