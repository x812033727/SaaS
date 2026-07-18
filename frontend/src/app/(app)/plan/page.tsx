"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type PlanCard = {
  key: string;
  label: string;
  monthly_price_cents: number;
  feature_labels: string[];
  is_current: boolean;
};

type Envelope = {
  plan_info: {
    effective: string;
    effective_label: string;
    paid: string;
    paid_label: string;
    trial_active: boolean;
    trial_days_left: number | null;
  };
  plans: PlanCard[];
};

type SubscribeResult = { mode: string; enabled: boolean; checkout_url: string | null };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

export default function PlanPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [checkoutUrl, setCheckoutUrl] = useState<string | null>(null);

  const data = useQuery({
    queryKey: ["plan"],
    queryFn: () => fetchJson<Envelope>("/api/v1/plan"),
    retry: false,
  });

  const subscribe = useMutation({
    mutationFn: (plan: string) => postJson<SubscribeResult>(`/api/v1/plan/${plan}/subscribe`, {}),
    onSuccess: (r) => {
      if (r.checkout_url) {
        setCheckoutUrl(r.checkout_url);
        window.open(r.checkout_url, "_blank", "noopener");
        setMsg({ kind: "ok", text: "請於新視窗完成綠界定期定額授權;完成首期授權後方案自動生效。" });
      } else {
        setMsg({ kind: "ok", text: "方案已生效。" });
      }
      qc.invalidateQueries({ queryKey: ["plan"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });
  const unsubscribe = useMutation({
    mutationFn: () => postJson<Envelope>("/api/v1/plan/unsubscribe", {}),
    onSuccess: (env) => {
      qc.setQueryData(["plan"], env);
      setMsg({ kind: "ok", text: "已退訂;已付費方案保留至最後扣款日+31 天。" });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  if (data.error instanceof ApiError && data.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">方案</h1>
        <p className="mt-6 rounded-xl border border-line bg-surface p-6 text-sm text-muted">
          方案管理僅限負責人。
        </p>
      </div>
    );
  }

  const env = data.data;

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">方案</h1>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {checkoutUrl && (
        <div className="mt-4 rounded-lg border border-line bg-ok-soft p-3 text-sm">
          <p>若新視窗未開啟,請
            <a href={checkoutUrl} target="_blank" rel="noopener noreferrer" className="mx-1 text-brand underline">
              點此前往綠界付款
            </a>
            完成授權。
          </p>
        </div>
      )}
      {data.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {env && (
        <>
          {env.plan_info.trial_active && (
            <p className="mt-4 rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn">
              試用中:{env.plan_info.effective_label},剩 {env.plan_info.trial_days_left} 天;
              試用結束後回到「{env.plan_info.paid_label}」。
            </p>
          )}
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {env.plans.map((p) => (
              <div key={p.key}
                className={`rounded-xl border p-5 ${p.is_current ? "border-brand bg-brand-soft/30" : "border-line bg-surface"}`}>
                <h2 className="font-semibold">
                  {p.label}
                  {p.is_current && <span className="ml-2 rounded-full bg-brand px-2 py-0.5 text-xs text-white">目前方案</span>}
                </h2>
                <p className="mt-1 text-2xl font-semibold">
                  {p.monthly_price_cents === 0 ? "免費" : `NT$${(p.monthly_price_cents / 100).toLocaleString()}/月`}
                </p>
                <ul className="mt-3 grid gap-1 text-sm text-muted">
                  {p.feature_labels.map((f) => <li key={f}>・{f}</li>)}
                  {p.feature_labels.length === 0 && <li>基本預約功能</li>}
                </ul>
                {p.key !== "free" && env.plan_info.paid !== p.key && (
                  <button
                    className="mt-4 w-full rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60"
                    disabled={subscribe.isPending}
                    onClick={() => subscribe.mutate(p.key)}>
                    訂閱{p.label}
                  </button>
                )}
              </div>
            ))}
          </div>
          {env.plan_info.paid !== "free" && (
            <div className="mt-4">
              <button
                className="rounded-lg border border-line px-4 py-2 text-sm text-danger hover:bg-danger-soft"
                disabled={unsubscribe.isPending}
                onClick={() => { if (confirm("退訂付費方案?已付費期間保留至最後扣款日+31 天。")) unsubscribe.mutate(); }}>
                退訂付費方案
              </button>
            </div>
          )}
          <p className="mt-4 text-sm text-muted">
            扣款明細與發票資訊請見<a href="/console/billing" className="mx-1 text-brand underline">帳單頁</a>;
            單一功能可於<a href="/console/features" className="mx-1 text-brand underline">進階功能頁</a>個別訂閱。
          </p>
        </>
      )}
    </div>
  );
}
