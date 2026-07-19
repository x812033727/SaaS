"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { fetchJson } from "@/lib/client-api";

type DayCell = {
  date: string;
  in_month?: boolean;
  reservations: { id: number; status: string; party_size: number }[];
};

type MonthView = {
  year: number;
  month: number;
  weeks: DayCell[][];
} & Record<string, unknown>;

type WeekView = { days: DayCell[] } & Record<string, unknown>;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`);
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function DayBox({ day, compact }: { day: DayCell; compact?: boolean }) {
  const confirmed = day.reservations.filter((r) => r.status === "confirmed");
  const to = addDays(day.date, 1);
  return (
    <Link
      href={`/reservations?date_from=${day.date}&date_to=${to}`}
      className={`block rounded-lg border border-line bg-surface p-2 transition hover:border-brand ${
        day.in_month === false ? "opacity-40" : ""
      } ${compact ? "min-h-16" : "min-h-20"}`}
    >
      <p className="text-xs text-muted">{day.date.slice(8)}</p>
      {confirmed.length > 0 && (
        <p className="mt-1 rounded bg-brand-soft px-1.5 py-0.5 text-xs font-medium text-brand">
          {confirmed.length} 筆預約
        </p>
      )}
    </Link>
  );
}

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

export default function CalendarPage() {
  const today = new Date();
  const [mode, setMode] = useState<"month" | "week">("month");
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth() + 1);
  const [anchor, setAnchor] = useState(
    `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`,
  );

  const monthQuery = useQuery({
    queryKey: ["calendar-month", year, month],
    queryFn: () => fetchJson<MonthView>(`/api/v1/calendar/month?year=${year}&month=${month}`),
    enabled: mode === "month",
  });
  const weekQuery = useQuery({
    queryKey: ["calendar-week", anchor],
    queryFn: () => fetchJson<WeekView>(`/api/v1/calendar/week?anchor=${anchor}`),
    enabled: mode === "week",
  });

  function shiftMonth(delta: number) {
    const total = year * 12 + (month - 1) + delta;
    setYear(Math.floor(total / 12));
    setMonth((total % 12) + 1);
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">行事曆</h1>
        <div className="flex items-center gap-2 text-sm">
          <div className="rounded-lg border border-line p-0.5">
            {(["month", "week"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)}
                className={`rounded-md px-3 py-1 ${mode === m ? "bg-brand text-white" : "text-muted"}`}>
                {m === "month" ? "月" : "週"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {mode === "month" ? (
        <>
          <div className="mt-4 flex items-center gap-3 text-sm">
            <button onClick={() => shiftMonth(-1)} className="rounded-md border border-line px-3 py-1">←</button>
            <p className="font-semibold">{year} 年 {month} 月</p>
            <button onClick={() => shiftMonth(1)} className="rounded-md border border-line px-3 py-1">→</button>
          </div>
          <div className="mt-3 grid grid-cols-7 gap-1 text-center text-xs text-muted">
            {WEEKDAYS.map((d) => <p key={d}>{d}</p>)}
          </div>
          {monthQuery.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}
          <div className="mt-1 grid gap-1">
            {(monthQuery.data?.weeks ?? []).map((week, i) => (
              <div key={i} className="grid grid-cols-7 gap-1">
                {week.map((day) => <DayBox key={day.date} day={day} compact />)}
              </div>
            ))}
          </div>
        </>
      ) : (
        <>
          <div className="mt-4 flex items-center gap-3 text-sm">
            <button onClick={() => setAnchor(addDays(anchor, -7))}
              className="rounded-md border border-line px-3 py-1">←</button>
            <p className="font-semibold">{anchor} 週</p>
            <button onClick={() => setAnchor(addDays(anchor, 7))}
              className="rounded-md border border-line px-3 py-1">→</button>
          </div>
          {weekQuery.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}
          <div className="mt-3 grid gap-1 sm:grid-cols-7">
            {(weekQuery.data?.days ?? []).map((day) => <DayBox key={day.date} day={day} />)}
          </div>
        </>
      )}
    </div>
  );
}
