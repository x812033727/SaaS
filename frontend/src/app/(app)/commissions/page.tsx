"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";
import { DataTable, type Column } from "@/components/ui/DataTable";

type NameRow = { id: number; name: string };
type TierRow = { threshold_cents: number; value: number };
type RuleRow = {
  id: number;
  staff_id: number;
  item_type: string;
  method: string;
  structure: string;
  value: number | null;
  calculation_basis: string;
  sales_period: string | null;
  effective_from: string;
  tiers: TierRow[];
};
type GoalRow = {
  goal_id: number;
  staff_id: number;
  item_type: string;
  target_cents: number;
  sales_period: string;
  actual_cents: number;
  percent: number;
  period_start: string;
  period_end: string;
};
type PayRunRow = {
  id: number;
  period_start: string;
  period_end: string;
  status: string;
  total_cents: number;
  created_at: string;
  finalized_at: string | null;
  paid_at: string | null;
};
type PayRunItemRow = {
  staff_id: number;
  staff_name: string;
  commission_cents: number;
  tip_cents: number;
  adjustment_cents: number;
  adjustment_note: string | null;
  total_cents: number;
};
type PayRunDetail = { run: PayRunRow; items: PayRunItemRow[] };
type EarningRow = {
  id: number;
  staff_id: number;
  item_type: string;
  item_name_snapshot: string;
  gross_cents: number;
  net_cents: number;
  commission_cents: number;
  earned_at: string;
  pay_run_id: number | null;
  reversed: boolean;
};
type Overview = {
  staff: NameRow[];
  rules: RuleRow[];
  goals: GoalRow[];
  pay_runs: PayRunRow[];
  recent_earnings: EarningRow[];
};

const STATUS_LABELS: Record<string, string> = { draft: "草稿", finalized: "已確認", paid: "已付款" };
const ITEM_LABELS: Record<string, string> = { service: "服務", product: "商品", all: "全部" };
const PERIOD_LABELS: Record<string, string> = {
  daily: "每日", weekly: "每週", biweekly: "雙週", four_week: "四週", monthly: "每月", quarterly: "每季",
};

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function twd(cents: number): string {
  return `NT$${(cents / 100).toLocaleString()}`;
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        員工抽成／薪資結算屬進階功能,請至
        <a href="/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

function TieredRuleForm({
  staff,
  today,
  onDone,
  onError,
}: {
  staff: NameRow[];
  today: string;
  onDone: () => void;
  onError: (e: unknown) => void;
}) {
  const [tierCount, setTierCount] = useState(2);

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/commissions/tiered-rules", body),
    onSuccess: onDone,
    onError,
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const f = new FormData(event.currentTarget);
    const tiers = [];
    for (let i = 0; i < tierCount; i += 1) {
      tiers.push({
        threshold_twd: String(f.get(`threshold_${i}`) ?? ""),
        value: String(f.get(`tier_value_${i}`) ?? ""),
      });
    }
    save.mutate({
      staff_id: Number(f.get("staff_id")),
      item_type: String(f.get("item_type")),
      method: String(f.get("method")),
      calculation_basis: String(f.get("calculation_basis")),
      sales_period: String(f.get("sales_period")),
      effective_from: String(f.get("effective_from")),
      tiers,
    });
  }

  return (
    <details className="mt-3 rounded-lg border border-line/60 p-3 text-sm">
      <summary className="cursor-pointer font-medium">階梯式規則(業績達門檻抽成跳級)</summary>
      <form className="mt-2 grid gap-2" onSubmit={submit}>
        <div className="grid grid-cols-2 gap-2">
          <label>員工
            <select name="staff_id" required className={inputCls}>
              {staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <label>類型
            <select name="item_type" className={inputCls}>
              <option value="service">服務</option>
              <option value="product">商品</option>
            </select>
          </label>
          <label>方式
            <select name="method" className={inputCls}>
              <option value="percent">百分比(%)</option>
              <option value="fixed">固定金額(NT$/件)</option>
            </select>
          </label>
          <label>計算基礎
            <select name="calculation_basis" className={inputCls}>
              <option value="net">折後淨額</option>
              <option value="gross">原價</option>
            </select>
          </label>
          <label>業績週期
            <select name="sales_period" className={inputCls}>
              <option value="monthly">每月</option>
              <option value="weekly">每週</option>
              <option value="biweekly">雙週</option>
              <option value="four_week">四週</option>
              <option value="daily">每日</option>
              <option value="quarterly">每季</option>
            </select>
          </label>
          <label>生效日<input name="effective_from" type="date" required defaultValue={today} className={inputCls} /></label>
        </div>
        {Array.from({ length: tierCount }, (_, i) => (
          <div key={i} className="grid grid-cols-2 gap-2">
            <label>門檻 {i + 1}(累計業績 NT$ ≥)
              <input name={`threshold_${i}`} required maxLength={32} placeholder={i === 0 ? "0" : ""} className={inputCls} />
            </label>
            <label>抽成值 {i + 1}
              <input name={`tier_value_${i}`} required maxLength={32} className={inputCls} />
            </label>
          </div>
        ))}
        <div className="flex gap-2">
          {tierCount < 10 && (
            <button type="button" className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20"
              onClick={() => setTierCount((n) => n + 1)}>+ 級距</button>
          )}
          {tierCount > 2 && (
            <button type="button" className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20"
              onClick={() => setTierCount((n) => n - 1)}>− 級距</button>
          )}
          <button disabled={save.isPending} className={btnCls}>儲存階梯規則</button>
        </div>
      </form>
    </details>
  );
}

export default function CommissionsPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const today = new Date().toISOString().slice(0, 10);

  const overview = useQuery({
    queryKey: ["commissions-overview"],
    queryFn: () => fetchJson<Overview>("/api/v1/commissions/overview"),
    retry: false,
  });
  const detail = useQuery({
    queryKey: ["commissions-pay-run", selectedRunId],
    queryFn: () => fetchJson<PayRunDetail>(`/api/v1/commissions/pay-runs/${selectedRunId}`),
    enabled: selectedRunId !== null,
    retry: false,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["commissions-overview"] });
    qc.invalidateQueries({ queryKey: ["commissions-pay-run"] });
  };
  const onOk = (text?: string) => {
    setMsg(text ? { kind: "ok", text } : null);
    refresh();
  };
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const saveRule = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/commissions/rules", body),
    onSuccess: () => onOk("抽成規則已儲存。"), onError: onErr,
  });
  const saveGoal = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/commissions/goals", body),
    onSuccess: () => onOk("業績目標已儲存。"), onError: onErr,
  });
  const createRun = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson<PayRunDetail>("/api/v1/commissions/pay-runs", body),
    onSuccess: (d) => { setSelectedRunId(d.run.id); onOk("結算單已建立(草稿)。"); },
    onError: onErr,
  });
  const adjustRun = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      postJson(`/api/v1/commissions/pay-runs/${input.id}/adjust`, input.body),
    onSuccess: () => onOk("加減項已更新。"), onError: onErr,
  });
  const transition = useMutation({
    mutationFn: (input: { id: number; action: "finalize" | "paid" | "delete" }) =>
      postJson(`/api/v1/commissions/pay-runs/${input.id}/${input.action}`, {}),
    onSuccess: (_d, input) => {
      if (input.action === "delete") setSelectedRunId(null);
      onOk(
        input.action === "finalize" ? "結算單已確認。"
          : input.action === "paid" ? "已標記付款。" : "草稿已刪除,明細釋回未結算池。",
      );
    },
    onError: onErr,
  });

  if (overview.error instanceof ApiError && overview.error.status === 403) {
    const locked = overview.error.detail.includes("Feature");
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">抽成／薪資</h1>
        <div className="mt-6">
          {locked ? <FeatureLockedCard /> : (
            <p className="rounded-xl border border-line bg-surface p-6 text-sm text-muted">
              抽成與薪資資料僅限負責人檢視。
            </p>
          )}
        </div>
      </div>
    );
  }

  const data = overview.data;
  const staffName = (id: number) => data?.staff.find((s) => s.id === id)?.name ?? `員工 #${id}`;

  const runColumns: Column<PayRunRow>[] = [
    { header: "#", cell: (r) => r.id },
    { header: "期間", cell: (r) => `${r.period_start} ～ ${r.period_end}` },
    {
      header: "狀態",
      cell: (r) => (
        <span className={r.status === "paid" ? "text-ok" : r.status === "finalized" ? "text-brand" : "text-muted"}>
          {STATUS_LABELS[r.status] ?? r.status}
        </span>
      ),
    },
    { header: "應付總額", className: "text-right", cell: (r) => twd(r.total_cents) },
  ];

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold">抽成／薪資</h1>
      <p className="mt-1 text-sm text-muted">
        設定員工抽成規則與業績目標;POS 結帳自動累計,期末建立結算單走 草稿 → 確認 → 付款。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {overview.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {data && (
        <>
          {/* ── 結算單 ── */}
          <section className="mt-6 rounded-xl border border-line bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">薪資結算單</h2>
              <form className="flex flex-wrap items-end gap-2 text-sm"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  createRun.mutate({
                    period_start: String(f.get("period_start")),
                    period_end: String(f.get("period_end")),
                  });
                }}>
                <label>期間起<input name="period_start" type="date" required className={inputCls} /></label>
                <label>期間迄<input name="period_end" type="date" required className={inputCls} /></label>
                <button disabled={createRun.isPending} className={btnCls}>建立結算單</button>
              </form>
            </div>
            <div className="mt-3">
              <DataTable columns={runColumns} rows={data.pay_runs} rowKey={(r) => r.id}
                emptyText="尚無結算單。" minWidth={520}
                onRowClick={(r) => setSelectedRunId(r.id)} />
            </div>
          </section>

          {/* ── 選定結算單 ── */}
          {selectedRunId !== null && detail.data && (
            <section className="mt-4 rounded-xl border border-brand/40 bg-surface p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-semibold">
                  結算單 #{detail.data.run.id}
                  <span className="ml-2 text-sm font-normal text-muted">
                    {detail.data.run.period_start} ～ {detail.data.run.period_end}・
                    {STATUS_LABELS[detail.data.run.status] ?? detail.data.run.status}・
                    {twd(detail.data.run.total_cents)}
                  </span>
                </h2>
                <div className="flex flex-wrap gap-2 text-sm">
                  <a className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20"
                    href={`/console/api/proxy/api/v1/commissions/pay-runs/${detail.data.run.id}/export.csv`}>
                    匯出 CSV
                  </a>
                  {detail.data.run.status === "draft" && (
                    <>
                      <button className={btnCls}
                        onClick={() => { if (confirm("確認結算單?確認後不可再調整加減項。")) transition.mutate({ id: detail.data!.run.id, action: "finalize" }); }}>
                        確認
                      </button>
                      <button className="rounded-lg border border-line px-3 py-1.5 text-danger hover:bg-danger-soft"
                        onClick={() => { if (confirm("刪除此草稿?明細將釋回未結算池。")) transition.mutate({ id: detail.data!.run.id, action: "delete" }); }}>
                        刪除草稿
                      </button>
                    </>
                  )}
                  {detail.data.run.status === "finalized" && (
                    <button className={btnCls}
                      onClick={() => { if (confirm("標記為已付款?此動作不可回復。")) transition.mutate({ id: detail.data!.run.id, action: "paid" }); }}>
                      標記已付款
                    </button>
                  )}
                  <button className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20"
                    onClick={() => setSelectedRunId(null)}>收合</button>
                </div>
              </div>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-sm" style={{ minWidth: 560 }}>
                  <thead>
                    <tr className="border-b border-line text-left text-muted">
                      <th className="px-3 py-2 font-medium">員工</th>
                      <th className="px-3 py-2 text-right font-medium">抽成</th>
                      <th className="px-3 py-2 text-right font-medium">小費</th>
                      <th className="px-3 py-2 text-right font-medium">加減項</th>
                      <th className="px-3 py-2 text-right font-medium">應付</th>
                      <th className="px-3 py-2 font-medium">說明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.items.map((it) => (
                      <tr key={it.staff_id} className="border-b border-line/60">
                        <td className="px-3 py-2">{it.staff_name}</td>
                        <td className="px-3 py-2 text-right">{twd(it.commission_cents)}</td>
                        <td className="px-3 py-2 text-right">{twd(it.tip_cents)}</td>
                        <td className="px-3 py-2 text-right">{twd(it.adjustment_cents)}</td>
                        <td className="px-3 py-2 text-right font-medium">{twd(it.total_cents)}</td>
                        <td className="px-3 py-2 text-muted">{it.adjustment_note ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {detail.data.run.status === "draft" && (
                <form className="mt-3 flex flex-wrap items-end gap-2 text-sm"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    adjustRun.mutate({
                      id: detail.data!.run.id,
                      body: {
                        staff_id: Number(f.get("staff_id")),
                        adjustment_twd: String(f.get("adjustment_twd") || "0"),
                        note: String(f.get("note") ?? ""),
                      },
                    });
                  }}>
                  <label>員工
                    <select name="staff_id" required className={inputCls}>
                      {detail.data.items.map((it) => (
                        <option key={it.staff_id} value={it.staff_id}>{it.staff_name}</option>
                      ))}
                    </select>
                  </label>
                  <label>加減項(NT$,可負)
                    <input name="adjustment_twd" required defaultValue="0" maxLength={32} className={`${inputCls} w-32`} />
                  </label>
                  <label>說明(選填)
                    <input name="note" maxLength={500} className={`${inputCls} w-48`} />
                  </label>
                  <button disabled={adjustRun.isPending} className={btnCls}>更新加減項</button>
                </form>
              )}
            </section>
          )}

          {/* ── 規則 + 目標 ── */}
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <section className="rounded-xl border border-line bg-surface p-4">
              <h2 className="font-semibold">抽成規則(現行)</h2>
              <ul className="mt-2 grid gap-1 text-sm">
                {data.rules.length === 0 && <li className="text-muted">尚無規則。</li>}
                {data.rules.map((r) => (
                  <li key={r.id}>
                    {staffName(r.staff_id)}・{ITEM_LABELS[r.item_type] ?? r.item_type}・
                    {r.structure === "tiered"
                      ? `階梯(${PERIOD_LABELS[r.sales_period ?? ""] ?? r.sales_period}) ${r.tiers
                          .map((t) => `≥${(t.threshold_cents / 100).toLocaleString()}→${r.method === "percent" ? `${t.value / 100}%` : twd(t.value)}`)
                          .join(" / ")}`
                      : r.method === "percent"
                        ? `${(r.value ?? 0) / 100}%`
                        : twd(r.value ?? 0)}
                    <span className="ml-1 text-xs text-muted">({r.calculation_basis === "net" ? "淨額" : "原價"};{r.effective_from} 起)</span>
                  </li>
                ))}
              </ul>
              <form className="mt-3 grid gap-2 text-sm"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  saveRule.mutate({
                    staff_id: Number(f.get("staff_id")),
                    item_type: String(f.get("item_type")),
                    method: String(f.get("method")),
                    value: String(f.get("value")),
                    calculation_basis: String(f.get("calculation_basis")),
                    effective_from: String(f.get("effective_from")),
                  });
                }}>
                <div className="grid grid-cols-2 gap-2">
                  <label>員工
                    <select name="staff_id" required className={inputCls}>
                      {data.staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </select>
                  </label>
                  <label>類型
                    <select name="item_type" className={inputCls}>
                      <option value="service">服務</option>
                      <option value="product">商品</option>
                    </select>
                  </label>
                  <label>方式
                    <select name="method" className={inputCls}>
                      <option value="percent">百分比(%)</option>
                      <option value="fixed">固定金額(NT$/件)</option>
                    </select>
                  </label>
                  <label>數值<input name="value" required maxLength={32} placeholder="如 10 或 150" className={inputCls} /></label>
                  <label>計算基礎
                    <select name="calculation_basis" className={inputCls}>
                      <option value="net">折後淨額</option>
                      <option value="gross">原價</option>
                    </select>
                  </label>
                  <label>生效日<input name="effective_from" type="date" required defaultValue={today} className={inputCls} /></label>
                </div>
                <div><button disabled={saveRule.isPending} className={btnCls}>儲存規則</button></div>
              </form>
              <TieredRuleForm staff={data.staff} today={today}
                onDone={() => onOk("階梯規則已儲存。")} onError={onErr} />
            </section>

            <section className="rounded-xl border border-line bg-surface p-4">
              <h2 className="font-semibold">業績目標進度</h2>
              <ul className="mt-2 grid gap-2 text-sm">
                {data.goals.length === 0 && <li className="text-muted">尚無目標。</li>}
                {data.goals.map((g) => (
                  <li key={g.goal_id}>
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span>
                        {staffName(g.staff_id)}・{ITEM_LABELS[g.item_type] ?? g.item_type}・
                        {PERIOD_LABELS[g.sales_period] ?? g.sales_period}
                      </span>
                      <span className="text-xs text-muted">
                        {twd(g.actual_cents)} / {twd(g.target_cents)}({g.percent.toFixed(0)}%)
                      </span>
                    </div>
                    <div className="mt-1 h-2 overflow-hidden rounded-full bg-line/40">
                      <div className="h-full rounded-full bg-brand" style={{ width: `${Math.min(100, g.percent)}%` }} />
                    </div>
                  </li>
                ))}
              </ul>
              <form className="mt-3 grid gap-2 text-sm"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  saveGoal.mutate({
                    staff_id: Number(f.get("staff_id")),
                    item_type: String(f.get("item_type")),
                    target_twd: String(f.get("target_twd")),
                    sales_period: String(f.get("sales_period")),
                    effective_from: String(f.get("effective_from")),
                  });
                }}>
                <div className="grid grid-cols-2 gap-2">
                  <label>員工
                    <select name="staff_id" required className={inputCls}>
                      {data.staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </select>
                  </label>
                  <label>範圍
                    <select name="item_type" className={inputCls}>
                      <option value="all">全部</option>
                      <option value="service">服務</option>
                      <option value="product">商品</option>
                    </select>
                  </label>
                  <label>目標(NT$)<input name="target_twd" required maxLength={32} className={inputCls} /></label>
                  <label>週期
                    <select name="sales_period" className={inputCls}>
                      <option value="monthly">每月</option>
                      <option value="weekly">每週</option>
                      <option value="biweekly">雙週</option>
                      <option value="four_week">四週</option>
                      <option value="daily">每日</option>
                      <option value="quarterly">每季</option>
                    </select>
                  </label>
                  <label>生效日<input name="effective_from" type="date" required defaultValue={today} className={inputCls} /></label>
                </div>
                <div><button disabled={saveGoal.isPending} className={btnCls}>儲存目標</button></div>
              </form>
            </section>
          </div>

          {/* ── 近期抽成明細 ── */}
          <section className="mt-4 rounded-xl border border-line bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">近期抽成明細</h2>
              <form className="flex flex-wrap items-end gap-2 text-xs"
                onSubmit={(e) => {
                  e.preventDefault();
                  const f = new FormData(e.currentTarget);
                  window.location.href =
                    `/console/api/proxy/api/v1/commissions/activity.csv?period_start=${f.get("ps")}&period_end=${f.get("pe")}`;
                }}>
                <label>起<input name="ps" type="date" required className="rounded-lg border border-line bg-surface px-2 py-1" /></label>
                <label>迄<input name="pe" type="date" required className="rounded-lg border border-line bg-surface px-2 py-1" /></label>
                <button className="rounded-lg border border-line px-2 py-1 hover:bg-line/20">匯出活動 CSV</button>
              </form>
            </div>
            <ul className="mt-2 grid gap-1 text-sm">
              {data.recent_earnings.length === 0 && <li className="text-muted">尚無明細。</li>}
              {data.recent_earnings.slice(0, 20).map((e) => (
                <li key={e.id} className={`flex flex-wrap items-baseline gap-2 ${e.reversed ? "line-through opacity-60" : ""}`}>
                  <span className="text-xs text-muted">{e.earned_at.slice(0, 16).replace("T", " ")}</span>
                  <span>{staffName(e.staff_id)}</span>
                  <span className="text-xs text-muted">{e.item_name_snapshot}</span>
                  <span className="font-medium">{twd(e.commission_cents)}</span>
                  {e.pay_run_id && <span className="text-xs text-muted">結算單 #{e.pay_run_id}</span>}
                  {e.reversed && <span className="text-xs text-danger">已沖銷</span>}
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
