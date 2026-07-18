"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type FeatureCard = {
  key: string;
  label: string;
  monthly_price_cents: number;
  enabled: boolean;
  subscription: {
    status: string;
    total_success_times: number;
    last_charged_at: string | null;
  } | null;
};

type ChargeRow = {
  id: number;
  feature_label: string;
  period_no: number;
  success: boolean;
  amount_cents: number;
  charged_at: string | null;
};

type Envelope = { features: FeatureCard[]; charges: ChargeRow[]; is_owner: boolean };

type SubscribeResult = { mode: string; enabled: boolean; checkout_url: string | null };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

export default function FeaturesPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [checkoutUrl, setCheckoutUrl] = useState<string | null>(null);

  const data = useQuery({
    queryKey: ["features"],
    queryFn: () => fetchJson<Envelope>("/api/v1/features"),
    retry: false,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["features"] });
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const subscribe = useMutation({
    mutationFn: (key: string) =>
      postJson<SubscribeResult>(`/api/v1/features/${key}/subscribe`, {}),
    onSuccess: (r) => {
      if (r.checkout_url) {
        // window.open 在 async callback 可能被彈窗攔截(noopener 下也偵測不到),
        // 一律留 fallback 連結,錢路不可斷
        setCheckoutUrl(r.checkout_url);
        window.open(r.checkout_url, "_blank", "noopener");
        setMsg({ kind: "ok", text: "請於新視窗完成綠界授權;完成首期授權後功能自動開通。" });
      } else {
        setCheckoutUrl(null);
        setMsg({ kind: "ok", text: "功能已開通。" });
      }
      refresh();
    },
    onError: onErr,
  });
  const unsubscribe = useMutation({
    mutationFn: (key: string) => postJson<Envelope>(`/api/v1/features/${key}/unsubscribe`, {}),
    onSuccess: (env) => {
      qc.setQueryData(["features"], env);
      setMsg({ kind: "ok", text: "已退訂,功能已關閉。" });
    },
    onError: onErr,
  });

  const env = data.data;

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold">進階功能</h1>
      <p className="mt-1 text-sm text-muted">
        單一功能月訂閱;<a href="/console/plan" className="text-brand underline">方案</a>內含的功能會直接顯示已開通。
      </p>
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
          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {env.features.map((f) => (
              <div key={f.key} className="rounded-xl border border-line bg-surface p-4 text-sm">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="font-semibold">{f.label}</h2>
                  <span className={`rounded-full px-2 py-0.5 text-xs ${f.enabled ? "bg-ok-soft text-ok" : "bg-line/40 text-muted"}`}>
                    {f.enabled ? "已開通" : "未開通"}
                  </span>
                </div>
                <p className="mt-1 text-muted">NT${(f.monthly_price_cents / 100).toLocaleString()}/月</p>
                {f.subscription && (
                  <p className="mt-1 text-xs text-muted">
                    {f.subscription.status}・成功扣款 {f.subscription.total_success_times} 期
                    {f.subscription.last_charged_at && `・最近 ${f.subscription.last_charged_at.slice(0, 10)}`}
                  </p>
                )}
                {f.subscription?.status === "cancel_failed" && (
                  <p className="mt-1 rounded bg-danger-soft px-2 py-1 text-xs text-danger">
                    停扣未確認:綠界停扣失敗,信用卡仍可能被扣款,系統將自動重試;如持續請聯絡客服。
                  </p>
                )}
                {env.is_owner && (
                  <button
                    className={`mt-3 w-full rounded-lg px-3 py-1.5 text-sm font-semibold ${f.enabled ? "border border-line text-danger hover:bg-danger-soft" : "bg-brand text-white hover:bg-brand-deep"} disabled:opacity-60`}
                    disabled={subscribe.isPending || unsubscribe.isPending}
                    onClick={() => {
                      if (f.enabled) {
                        if (confirm(`退訂「${f.label}」?功能將立即關閉。`)) unsubscribe.mutate(f.key);
                      } else {
                        subscribe.mutate(f.key);
                      }
                    }}>
                    {f.enabled ? "退訂" : "訂閱"}
                  </button>
                )}
              </div>
            ))}
          </div>

          <section className="mt-6 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">扣款紀錄(最近 20 筆)</h2>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-sm" style={{ minWidth: 520 }}>
                <thead>
                  <tr className="border-b border-line text-left text-muted">
                    <th className="px-3 py-2 font-medium">#</th>
                    <th className="px-3 py-2 font-medium">項目</th>
                    <th className="px-3 py-2 font-medium">期數</th>
                    <th className="px-3 py-2 text-right font-medium">金額</th>
                    <th className="px-3 py-2 font-medium">結果</th>
                    <th className="px-3 py-2 font-medium">時間</th>
                  </tr>
                </thead>
                <tbody>
                  {env.charges.length === 0 && (
                    <tr><td colSpan={6} className="px-3 py-6 text-center text-muted">尚無扣款紀錄。</td></tr>
                  )}
                  {env.charges.map((c) => (
                    <tr key={c.id} className="border-b border-line/60">
                      <td className="px-3 py-2">{c.id}</td>
                      <td className="px-3 py-2">{c.feature_label}</td>
                      <td className="px-3 py-2">#{c.period_no}</td>
                      <td className="px-3 py-2 text-right">NT${(c.amount_cents / 100).toLocaleString()}</td>
                      <td className="px-3 py-2">
                        <span className={c.success ? "text-ok" : "text-danger"}>{c.success ? "成功" : "失敗"}</span>
                      </td>
                      <td className="px-3 py-2 text-muted">{c.charged_at?.slice(0, 10) ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
