"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  // 2FA 第二步:後端回 otp_required 時記住帳密、切到 OTP 輸入畫面。
  const [otpStep, setOtpStep] = useState<{ email: string; password: string } | null>(null);

  async function login(email: string, password: string, otp?: string) {
    setPending(true);
    setError("");
    // 注意:client fetch 不會自動吃 next.config 的 basePath,要寫全路徑。
    const response = await fetch("/console/api/session/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, ...(otp ? { otp } : {}) }),
    });
    setPending(false);
    if (!response.ok) {
      let code = "";
      try {
        code = ((await response.json()) as { error?: string }).error ?? "";
      } catch {
        // ignore body parse errors
      }
      if (code === "otp_required") {
        setOtpStep({ email, password });
        return;
      }
      if (code === "otp_invalid") {
        setOtpStep({ email, password });
        setError("驗證碼錯誤，請重新輸入。");
        return;
      }
      setOtpStep(null);
      setError("登入失敗，請確認帳號與密碼。");
      return;
    }
    router.replace("/dashboard");
    router.refresh();
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await login(String(form.get("email") ?? ""), String(form.get("password") ?? ""));
  }

  async function submitOtp(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!otpStep) return;
    const form = new FormData(event.currentTarget);
    await login(otpStep.email, otpStep.password, String(form.get("otp") ?? ""));
  }

  return (
    <main className="grid min-h-screen place-items-center px-5 py-10">
      <section className="w-full max-w-md rounded-3xl border border-[var(--line)] bg-[var(--surface)] p-8 shadow-xl shadow-black/5">
        <p className="mb-2 text-sm font-semibold tracking-[0.18em] text-[var(--accent)]">SERVICE OS</p>
        {otpStep ? (
          <>
            <h1 className="text-3xl font-semibold">兩步驟驗證</h1>
            <p className="mt-2 text-sm text-[var(--muted)]">
              請輸入驗證器 App 顯示的 6 位數驗證碼，或一組未使用過的恢復碼。
            </p>
            <form className="mt-8 grid gap-5" onSubmit={submitOtp}>
              <label className="grid gap-2 text-sm font-medium">
                驗證碼
                <input
                  className="rounded-xl border border-[var(--line)] px-4 py-3"
                  name="otp"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={16}
                  autoFocus
                  required
                />
              </label>
              {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
              <button disabled={pending} className="rounded-xl bg-[var(--brand)] px-4 py-3 font-semibold text-white hover:bg-[var(--brand-strong)] disabled:opacity-60">
                {pending ? "驗證中…" : "驗證並登入"}
              </button>
              <button
                type="button"
                className="text-sm text-[var(--muted)] underline"
                onClick={() => {
                  setOtpStep(null);
                  setError("");
                }}
              >
                回登入頁
              </button>
            </form>
          </>
        ) : (
          <>
            <h1 className="text-3xl font-semibold">歡迎回來</h1>
            <p className="mt-2 text-sm text-[var(--muted)]">登入管理預約、顧客與門店營運。</p>
            <form className="mt-8 grid gap-5" onSubmit={submit}>
              <label className="grid gap-2 text-sm font-medium">
                Email
                <input className="rounded-xl border border-[var(--line)] px-4 py-3" name="email" type="email" autoComplete="email" required />
              </label>
              <label className="grid gap-2 text-sm font-medium">
                密碼
                <input className="rounded-xl border border-[var(--line)] px-4 py-3" name="password" type="password" autoComplete="current-password" required />
              </label>
              {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
              <button disabled={pending} className="rounded-xl bg-[var(--brand)] px-4 py-3 font-semibold text-white hover:bg-[var(--brand-strong)] disabled:opacity-60">
                {pending ? "登入中…" : "登入"}
              </button>
            </form>
          </>
        )}
      </section>
    </main>
  );
}
