"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson } from "@/lib/client-api";

type LineConfig = {
  has_channel_secret: boolean;
  has_access_token: boolean;
  default_target_lang: string;
  bot_mode: string;
  credential_status: string;
  credential_last_error: string | null;
  credential_checked_at: string | null;
  webhook_url: string;
};

type VerifyResult = LineConfig;
type WebhookResult = {
  endpoint: string; success: boolean; active: boolean | null;
  status_code: number | null; reason: string | null; detail: string | null;
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

const STATUS_STYLE: Record<string, string> = {
  valid: "bg-ok-soft text-ok",
  invalid: "bg-danger-soft text-danger",
  unchecked: "bg-line text-muted",
};

export default function LineSettingsPage() {
  const qc = useQueryClient();
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [webhookResult, setWebhookResult] = useState<WebhookResult | null>(null);

  const config = useQuery({
    queryKey: ["line-config"],
    queryFn: async () => {
      try {
        return await fetchJson<LineConfig>("/tenants/me/line-config");
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null; // 尚未設定
        throw e;
      }
    },
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["line-config"] });

  const saveMut = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      fetchJson("/tenants/me/line-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    onSuccess: () => { invalidate(); setMessage({ kind: "ok", text: "已儲存並驗證憑證。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const verifyMut = useMutation({
    mutationFn: () => fetchJson<VerifyResult>("/tenants/me/line-config/verify", { method: "POST" }),
    onSuccess: () => { invalidate(); setMessage({ kind: "ok", text: "已重新驗證。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const webhookMut = useMutation({
    mutationFn: () => fetchJson<WebhookResult>("/tenants/me/line-config/webhook/setup", { method: "POST" }),
    onSuccess: (r) => {
      setWebhookResult(r);
      setMessage({ kind: r.success ? "ok" : "error", text: r.success ? "Webhook 已設定並測試通過。" : "Webhook 設定/測試未通過,詳見下方。" });
    },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    saveMut.mutate({
      channel_secret: String(form.get("channel_secret") || "").trim(),
      access_token: String(form.get("access_token") || "").trim(),
      bot_mode: String(form.get("bot_mode") || "translation"),
    });
  }

  const cfg = config.data;
  const configured = !!cfg?.has_channel_secret && !!cfg?.has_access_token;

  return (
    <div className="mx-auto max-w-3xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">LINE 設定</h1>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <section className="mt-4 rounded-xl border border-line bg-surface p-6">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Channel 憑證</h2>
          {cfg && (
            <span className={`rounded-full px-2.5 py-0.5 text-xs ${STATUS_STYLE[cfg.credential_status] ?? "bg-line text-muted"}`}>
              {cfg.credential_status === "valid" ? "已驗證" : cfg.credential_status === "invalid" ? "驗證失敗" : "未驗證"}
            </span>
          )}
        </div>
        {cfg?.credential_last_error && (
          <p className="mt-2 text-xs text-danger">{cfg.credential_last_error}</p>
        )}
        <form className="mt-4 grid gap-3 text-sm" onSubmit={submit}>
          <label className="grid gap-1">
            Channel Secret {configured && <span className="text-xs text-muted">(已設定,更新需重新輸入)</span>}
            <input name="channel_secret" required maxLength={64}
              placeholder={cfg?.has_channel_secret ? "•••••••• 已設定" : "填入 Messaging API Channel Secret"}
              className="rounded-lg border border-line px-3 py-2 font-mono" />
          </label>
          <label className="grid gap-1">
            Channel Access Token {configured && <span className="text-xs text-muted">(已設定,更新需重新輸入)</span>}
            <input name="access_token" required maxLength={1024}
              placeholder={cfg?.has_access_token ? "•••••••• 已設定" : "填入 long-lived access token"}
              className="rounded-lg border border-line px-3 py-2 font-mono" />
          </label>
          <label className="grid gap-1">
            Bot 模式
            <select name="bot_mode" defaultValue={cfg?.bot_mode ?? "translation"}
              className="rounded-lg border border-line px-3 py-2">
              <option value="translation">翻譯</option>
              <option value="booking">預約</option>
            </select>
          </label>
          <div className="flex gap-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : "儲存並驗證"}
            </button>
            {configured && (
              <button type="button" onClick={() => verifyMut.mutate()} disabled={verifyMut.isPending}
                className="rounded-lg border border-line px-4 py-2 hover:bg-brand-soft">
                重新驗證
              </button>
            )}
          </div>
        </form>
        <p className="mt-3 text-xs text-muted">
          憑證加密保存,頁面永遠只顯示遮罩,不回傳明文。
        </p>
      </section>

      <section className="mt-4 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">Webhook</h2>
        {cfg && (
          <p className="mt-2 break-all rounded-lg bg-canvas px-3 py-2 font-mono text-xs">{cfg.webhook_url}</p>
        )}
        <button onClick={() => webhookMut.mutate()} disabled={!configured || webhookMut.isPending}
          className="mt-3 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-50">
          {webhookMut.isPending ? "設定中…" : "一鍵設定並測試 Webhook"}
        </button>
        {!configured && <p className="mt-2 text-xs text-muted">請先設定並驗證 Channel 憑證。</p>}
        {webhookResult && (
          <div className={`mt-3 rounded-lg p-3 text-sm ${webhookResult.success ? "bg-ok-soft" : "bg-danger-soft"}`}>
            <p className={webhookResult.success ? "text-ok" : "text-danger"}>
              {webhookResult.success ? "✓ 設定成功" : "✗ 未通過"}
              {webhookResult.active != null && ` · webhook ${webhookResult.active ? "啟用中" : "未啟用"}`}
              {webhookResult.status_code != null && ` · HTTP ${webhookResult.status_code}`}
            </p>
            {webhookResult.reason && <p className="mt-1 text-xs text-muted">{webhookResult.reason}</p>}
          </div>
        )}
      </section>
    </div>
  );
}
