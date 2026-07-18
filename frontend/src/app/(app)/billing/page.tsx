"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type ChargeRow = {
  id: number;
  period_no: number;
  success: boolean;
  amount_cents: number;
  charged_at: string | null;
  rtn_msg: string | null;
  invoice_status: string | null;
  invoice_no: string | null;
};

type Envelope = {
  plan_info: { effective_label: string; trial_active: boolean; trial_days_left: number | null };
  subscription: {
    feature: string;
    label: string;
    status: string;
    period_amount_cents: number;
    next_charge_at: string | null;
  } | null;
  charges: ChargeRow[];
  einvoice_config: {
    configured: boolean;
    merchant_id: string;
    environment: string;
    enabled: boolean;
    has_hash_key: boolean;
    has_hash_iv: boolean;
  };
  invoice_profile: {
    configured: boolean;
    mode: string | null;
    buyer_name: string | null;
    buyer_identifier: string | null;
    carrier_type: string | null;
    has_carrier_number: boolean;
    masked_carrier: string | null;
    donation_code: string | null;
  };
};

const INVOICE_STATUS_LABELS: Record<string, string> = {
  pending: "開立處理中", issued: "已開立", failed: "開立處理中", voiding: "作廢中", void: "已作廢",
};

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

export default function BillingPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [profileMode, setProfileMode] = useState<string | null>(null);

  const data = useQuery({
    queryKey: ["billing"],
    queryFn: () => fetchJson<Envelope>("/api/v1/billing"),
    retry: false,
  });

  const onEnv = (env: Envelope, text: string) => {
    qc.setQueryData(["billing"], env);
    setMsg({ kind: "ok", text });
  };
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const saveProfile = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson<Envelope>("/api/v1/billing/invoice-profile", body),
    onSuccess: (env) => onEnv(env, "發票資料已更新。"),
    onError: onErr,
  });
  const saveEinvoice = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson<Envelope>("/api/v1/billing/einvoice-config", body),
    onSuccess: (env) => onEnv(env, "電子發票設定已更新。"),
    onError: onErr,
  });

  if (data.error instanceof ApiError && data.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">帳單</h1>
        <p className="mt-6 rounded-xl border border-line bg-surface p-6 text-sm text-muted">
          帳單僅限負責人檢視。
        </p>
      </div>
    );
  }

  const env = data.data;
  const mode = profileMode ?? env?.invoice_profile.mode ?? "personal";

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">帳單</h1>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {data.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {env && (
        <div className="mt-6 grid gap-4">
          {/* 方案摘要 */}
          <section className="rounded-xl border border-line bg-surface p-4 text-sm">
            <h2 className="font-semibold">目前方案</h2>
            <p className="mt-2">
              {env.plan_info.effective_label}
              {env.plan_info.trial_active && (
                <span className="ml-2 rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn">
                  試用中,剩 {env.plan_info.trial_days_left} 天
                </span>
              )}
            </p>
            {env.subscription ? (
              <p className="mt-1 text-muted">
                {env.subscription.label}・{env.subscription.status}・
                NT${(env.subscription.period_amount_cents / 100).toLocaleString()}/月
                {env.subscription.next_charge_at &&
                  `・下次扣款約 ${env.subscription.next_charge_at.slice(0, 10)}`}
              </p>
            ) : (
              <p className="mt-1 text-muted">
                尚無訂閱;前往<a href="/plan" className="mx-1 text-brand underline">方案頁</a>選購。
              </p>
            )}
          </section>

          {/* 平台發票買受資訊 */}
          <section className="rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">發票資料(平台開立給您的月費發票)</h2>
            <p className="mt-0.5 text-xs text-muted">
              {env.invoice_profile.configured
                ? `目前:${mode === "business" ? `統編 ${env.invoice_profile.buyer_identifier ?? ""}(${env.invoice_profile.buyer_name ?? ""})` : mode === "donation" ? `捐贈 ${env.invoice_profile.donation_code ?? ""}` : `個人${env.invoice_profile.masked_carrier ? `・載具 ${env.invoice_profile.masked_carrier}` : ""}`}`
                : "尚未設定,預設開立個人電子發票。"}
            </p>
            <form className="mt-3 grid gap-3 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                saveProfile.mutate({
                  mode,
                  buyer_name: String(f.get("buyer_name") ?? ""),
                  buyer_identifier: String(f.get("buyer_identifier") ?? ""),
                  carrier_type: String(f.get("carrier_type") ?? "ecpay"),
                  carrier_number: String(f.get("carrier_number") ?? ""),
                  donation_code: String(f.get("donation_code") ?? ""),
                });
              }}>
              <label className="max-w-xs">類型
                <select value={mode} onChange={(e) => setProfileMode(e.target.value)} className={inputCls}>
                  <option value="personal">個人</option>
                  <option value="business">公司(統編)</option>
                  <option value="donation">捐贈</option>
                </select>
              </label>
              {mode === "business" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <label>公司抬頭
                    <input name="buyer_name" maxLength={60} defaultValue={env.invoice_profile.buyer_name ?? ""} className={inputCls} />
                  </label>
                  <label>統一編號
                    <input name="buyer_identifier" maxLength={8} defaultValue={env.invoice_profile.buyer_identifier ?? ""} className={inputCls} />
                  </label>
                </div>
              )}
              {mode === "personal" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <label>載具類型
                    <select name="carrier_type" defaultValue={env.invoice_profile.carrier_type ?? "ecpay"} className={inputCls}>
                      <option value="ecpay">綠界會員載具</option>
                      <option value="mobile">手機條碼</option>
                      <option value="citizen">自然人憑證</option>
                    </select>
                  </label>
                  <label>載具號碼
                    <input name="carrier_number" maxLength={64}
                      placeholder={env.invoice_profile.has_carrier_number ? `已加密儲存 ${env.invoice_profile.masked_carrier ?? ""};留空保留` : ""}
                      className={inputCls} />
                  </label>
                </div>
              )}
              {mode === "donation" && (
                <label className="max-w-xs">愛心碼
                  <input name="donation_code" maxLength={7} defaultValue={env.invoice_profile.donation_code ?? ""} className={inputCls} />
                </label>
              )}
              <div><button disabled={saveProfile.isPending} className={btnCls}>儲存發票資料</button></div>
            </form>
          </section>

          {/* 店家自有電子發票 */}
          <section className="rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">電子發票(您開立給顧客,選用)</h2>
            <p className="mt-0.5 text-xs text-muted">
              填入您自己的綠界電子發票商店憑證後,POS 結帳可自動為顧客開立發票。憑證加密保存、留空=沿用既有值。
            </p>
            <form className="mt-3 grid gap-3 text-sm sm:grid-cols-2"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                saveEinvoice.mutate({
                  merchant_id: String(f.get("merchant_id") ?? ""),
                  hash_key: String(f.get("hash_key") ?? ""),
                  hash_iv: String(f.get("hash_iv") ?? ""),
                  environment: String(f.get("environment") ?? "stage"),
                  enabled: f.get("enabled") === "on",
                });
              }}>
              <label>MerchantID
                <input name="merchant_id" maxLength={20} defaultValue={env.einvoice_config.merchant_id} className={inputCls} />
              </label>
              <label>環境
                <select name="environment" defaultValue={env.einvoice_config.environment} className={inputCls}>
                  <option value="stage">測試(stage)</option>
                  <option value="prod">正式(prod)</option>
                </select>
              </label>
              <label>HashKey
                <input name="hash_key" maxLength={64} type="password"
                  placeholder={env.einvoice_config.has_hash_key ? "已加密儲存;留空保留" : ""} className={inputCls} />
              </label>
              <label>HashIV
                <input name="hash_iv" maxLength={64} type="password"
                  placeholder={env.einvoice_config.has_hash_iv ? "已加密儲存;留空保留" : ""} className={inputCls} />
              </label>
              <label className="flex items-center gap-2 sm:col-span-2">
                <input name="enabled" type="checkbox" defaultChecked={env.einvoice_config.enabled} />
                啟用自動開立
              </label>
              <div className="sm:col-span-2">
                <button disabled={saveEinvoice.isPending} className={btnCls}>儲存電子發票設定</button>
              </div>
            </form>
          </section>

          {/* 扣款明細 */}
          <section className="rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">扣款明細(最近 24 期)</h2>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-sm" style={{ minWidth: 560 }}>
                <thead>
                  <tr className="border-b border-line text-left text-muted">
                    <th className="px-3 py-2 font-medium">期數</th>
                    <th className="px-3 py-2 font-medium">結果</th>
                    <th className="px-3 py-2 text-right font-medium">金額</th>
                    <th className="px-3 py-2 font-medium">扣款日</th>
                    <th className="px-3 py-2 font-medium">發票</th>
                  </tr>
                </thead>
                <tbody>
                  {env.charges.length === 0 && (
                    <tr><td colSpan={5} className="px-3 py-6 text-center text-muted">尚無扣款紀錄。</td></tr>
                  )}
                  {env.charges.map((c) => (
                    <tr key={c.id} className="border-b border-line/60">
                      <td className="px-3 py-2">#{c.period_no}</td>
                      <td className="px-3 py-2">
                        <span className={c.success ? "text-ok" : "text-danger"}>
                          {c.success ? "成功" : "失敗"}
                        </span>
                        {c.rtn_msg && <span className="ml-1 text-xs text-muted">{c.rtn_msg}</span>}
                      </td>
                      <td className="px-3 py-2 text-right">NT${(c.amount_cents / 100).toLocaleString()}</td>
                      <td className="px-3 py-2 text-muted">{c.charged_at?.slice(0, 10) ?? "—"}</td>
                      <td className="px-3 py-2 text-muted">
                        {c.invoice_status
                          ? c.invoice_status === "issued" && c.invoice_no
                            ? c.invoice_no
                            : INVOICE_STATUS_LABELS[c.invoice_status] ?? c.invoice_status
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
