"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type Summary = {
  email: string;
  email_verified: boolean;
  last_login_at: string | null;
  last_login_ip: string | null;
  totp_enabled: boolean;
  remaining_recovery_codes: number;
  oauth_provider: string | null;
  line_login_configured: boolean;
};

type TotpStart = { qr_svg: string; secret: string; otpauth_uri: string };

const PROVIDER_LABELS: Record<string, string> = { line: "LINE", google: "Google" };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

async function reauth(action: "password" | "logout-all", body: Record<string, unknown>) {
  const res = await fetch("/console/api/session/reauth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, body }),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = ((await res.json()) as { detail?: string }).detail ?? ""; } catch { /* noop */ }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2";
const btnCls =
  "rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

export default function AccountPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [totpStart, setTotpStart] = useState<TotpStart | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);

  const summary = useQuery({
    queryKey: ["account"],
    queryFn: () => fetchJson<Summary>("/api/v1/account"),
    retry: false,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["account"] });
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const changePassword = useMutation({
    mutationFn: (body: Record<string, unknown>) => reauth("password", body),
    onSuccess: () => setMsg({ kind: "ok", text: "密碼已更新;其他裝置的登入已全部失效。" }),
    onError: onErr,
  });
  const logoutAll = useMutation({
    mutationFn: () => reauth("logout-all", {}),
    onSuccess: () => setMsg({ kind: "ok", text: "已登出所有其他裝置;本裝置維持登入。" }),
    onError: onErr,
  });
  const totpStartMut = useMutation({
    mutationFn: () => postJson<TotpStart>("/api/v1/account/totp/start", {}),
    onSuccess: (r) => { setTotpStart(r); setMsg(null); },
    onError: onErr,
  });
  const totpConfirm = useMutation({
    mutationFn: (otp: string) =>
      postJson<{ recovery_codes: string[] }>("/api/v1/account/totp/confirm", { otp }),
    onSuccess: (r) => {
      setTotpStart(null);
      setRecoveryCodes(r.recovery_codes);
      setMsg({ kind: "ok", text: "兩步驟驗證已啟用。" });
      refresh();
    },
    onError: onErr,
  });
  const totpDisable = useMutation({
    mutationFn: (otp: string) => postJson("/api/v1/account/totp/disable", { otp }),
    onSuccess: () => { setRecoveryCodes(null); setMsg({ kind: "ok", text: "兩步驟驗證已停用。" }); refresh(); },
    onError: onErr,
  });
  const unlink = useMutation({
    mutationFn: () => postJson("/api/v1/account/oauth/unlink", {}),
    onSuccess: () => { setMsg({ kind: "ok", text: "已解除社群帳號連結。" }); refresh(); },
    onError: onErr,
  });
  const resend = useMutation({
    mutationFn: () => postJson<{ outcome: string }>("/api/v1/account/resend-verification", {}),
    onSuccess: (r) =>
      setMsg({ kind: "ok", text: r.outcome === "sent" ? "驗證信已寄出。" : "驗證信已排入寄送佇列。" }),
    onError: onErr,
  });

  const s = summary.data;

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold">帳號設定</h1>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {summary.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {s && (
        <div className="mt-6 grid gap-4">
          {/* 登入活動 */}
          <section className="rounded-xl border border-line bg-surface p-4 text-sm">
            <h2 className="font-semibold">登入活動</h2>
            <p className="mt-2 text-muted">
              {s.email}
              {s.email_verified ? (
                <span className="ml-2 rounded-full bg-ok-soft px-2 py-0.5 text-xs text-ok">已驗證</span>
              ) : (
                <>
                  <span className="ml-2 rounded-full bg-warn-soft px-2 py-0.5 text-xs text-warn">未驗證</span>
                  <button className="ml-2 text-xs text-brand hover:underline"
                    disabled={resend.isPending} onClick={() => resend.mutate()}>
                    重寄驗證信
                  </button>
                </>
              )}
            </p>
            <p className="mt-1 text-muted">
              上次登入:
              {s.last_login_at
                ? new Date(s.last_login_at.endsWith("Z") ? s.last_login_at : `${s.last_login_at}Z`)
                    .toLocaleString("zh-TW", { timeZone: "Asia/Taipei" })
                : "—"}
              {s.last_login_ip ? `(IP ${s.last_login_ip})` : ""}
            </p>
          </section>

          {/* 變更密碼 */}
          <section className="rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">變更密碼</h2>
            <p className="mt-0.5 text-xs text-muted">變更後其他裝置的登入立即失效;本裝置維持登入。</p>
            <form className="mt-3 grid gap-3 text-sm sm:grid-cols-3"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                changePassword.mutate({
                  current_password: String(f.get("current_password")),
                  new_password: String(f.get("new_password")),
                  confirm_password: String(f.get("confirm_password")),
                });
                e.currentTarget.reset();
              }}>
              <label>目前密碼
                <input name="current_password" type="password" required className={inputCls} />
              </label>
              <label>新密碼(至少 8 字元)
                <input name="new_password" type="password" required minLength={8} className={inputCls} />
              </label>
              <label>確認新密碼
                <input name="confirm_password" type="password" required className={inputCls} />
              </label>
              <div className="sm:col-span-3">
                <button disabled={changePassword.isPending} className={btnCls}>更新密碼</button>
              </div>
            </form>
          </section>

          {/* 2FA */}
          <section className="rounded-xl border border-line bg-surface p-4 text-sm">
            <h2 className="font-semibold">兩步驟驗證(TOTP)</h2>
            {recoveryCodes && (
              <div className="mt-3 rounded-lg border border-line bg-ok-soft p-3">
                <p className="font-semibold text-ok">恢復碼(僅顯示這一次,請立即保存):</p>
                <div className="mt-1 grid grid-cols-2 gap-1 font-mono text-xs sm:grid-cols-5">
                  {recoveryCodes.map((c) => <code key={c}>{c}</code>)}
                </div>
              </div>
            )}
            {!s.totp_enabled && !totpStart && (
              <div className="mt-3">
                <p className="text-muted">以驗證 App(Google Authenticator 等)產生一次性驗證碼,登入需第二步驗證。</p>
                <button className={`mt-2 ${btnCls}`} disabled={totpStartMut.isPending}
                  onClick={() => totpStartMut.mutate()}>
                  啟用兩步驟驗證
                </button>
              </div>
            )}
            {!s.totp_enabled && totpStart && (
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div>
                  {/* QR SVG 以 data: URI 走 <img>:img 情境不執行內嵌 script,較 innerHTML 安全 */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    alt="TOTP QR code"
                    className="max-w-[200px] rounded-lg border border-line bg-white p-2"
                    src={`data:image/svg+xml;base64,${btoa(totpStart.qr_svg)}`}
                  />
                  <p className="mt-1 break-all text-xs text-muted">手動輸入:{totpStart.secret}</p>
                </div>
                <form className="grid content-start gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    totpConfirm.mutate(String(new FormData(e.currentTarget).get("otp")));
                  }}>
                  <label>掃描 QR 後輸入 6 位數驗證碼
                    <input name="otp" required inputMode="numeric" maxLength={8} className={inputCls} />
                  </label>
                  <div className="flex gap-2">
                    <button disabled={totpConfirm.isPending} className={btnCls}>確認啟用</button>
                    <button type="button" className="rounded-lg border border-line px-3 py-2 hover:bg-line/20"
                      onClick={() => setTotpStart(null)}>取消</button>
                  </div>
                </form>
              </div>
            )}
            {s.totp_enabled && (
              <div className="mt-3">
                <p className="text-ok">已啟用。剩餘恢復碼:{s.remaining_recovery_codes} 組。</p>
                <form className="mt-2 flex flex-wrap items-end gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    totpDisable.mutate(String(new FormData(e.currentTarget).get("otp")));
                    e.currentTarget.reset();
                  }}>
                  <label>驗證碼或恢復碼
                    <input name="otp" required className={inputCls} />
                  </label>
                  <button disabled={totpDisable.isPending}
                    className="rounded-lg border border-line px-3 py-2 text-danger hover:bg-danger-soft">
                    停用
                  </button>
                </form>
              </div>
            )}
          </section>

          {/* 工作階段 */}
          <section className="rounded-xl border border-line bg-surface p-4 text-sm">
            <h2 className="font-semibold">工作階段</h2>
            <p className="mt-0.5 text-xs text-muted">懷疑帳號外洩時,一鍵撤銷所有裝置的登入(本裝置除外)。</p>
            <button className="mt-2 rounded-lg border border-line px-4 py-2 text-danger hover:bg-danger-soft"
              disabled={logoutAll.isPending}
              onClick={() => { if (confirm("登出所有其他裝置?")) logoutAll.mutate(); }}>
              登出所有裝置
            </button>
          </section>

          {/* 社群帳號 */}
          <section className="rounded-xl border border-line bg-surface p-4 text-sm">
            <h2 className="font-semibold">社群帳號連結</h2>
            {s.oauth_provider ? (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <span className="text-muted">
                  已連結 {PROVIDER_LABELS[s.oauth_provider] ?? s.oauth_provider},可用社群帳號快速登入。
                </span>
                <button className="text-danger hover:underline"
                  onClick={() => { if (confirm("解除連結?之後僅能以密碼登入。")) unlink.mutate(); }}>
                  解除連結
                </button>
              </div>
            ) : s.line_login_configured ? (
              <div className="mt-2">
                <a href="/auth/oauth/line/login?link=1"
                  className="inline-block rounded-lg bg-[#06C755] px-4 py-2 font-semibold text-white hover:opacity-90">
                  連結 LINE 帳號
                </a>
                <p className="mt-1 text-xs text-muted">授權完成後會導回舊版帳號頁,連結即生效。</p>
              </div>
            ) : (
              <p className="mt-2 text-muted">平台尚未設定 LINE Login,暫無法連結社群帳號。</p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
