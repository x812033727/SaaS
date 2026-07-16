import { NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

// cookie 壽命對齊後端 JWT 壽命(SAAS_ACCESS_TOKEN_EXPIRE_MINUTES,預設 60 分)。
// 舊版 maxAge 8h 但 JWT 只活 60 分 → cookie 還在、token 已死的殭屍態。
const tokenMaxAgeSeconds =
  Number(process.env.SAAS_ACCESS_TOKEN_EXPIRE_MINUTES ?? "60") * 60;

const secure = process.env.NODE_ENV === "production";

function randomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Buffer.from(bytes).toString("base64url");
}

export async function POST(request: Request) {
  const body = (await request.json()) as { email?: string; password?: string };
  if (!body.email || !body.password) return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  const form = new URLSearchParams({ username: body.email, password: body.password });
  const upstream = await fetch(`${apiOrigin}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
    cache: "no-store",
  });
  if (!upstream.ok) return NextResponse.json({ error: "invalid_credentials" }, { status: 401 });
  const token = (await upstream.json()) as { access_token: string };
  const response = NextResponse.json({ ok: true });
  const common = { secure, sameSite: "lax" as const, path: "/", maxAge: tokenMaxAgeSeconds };
  // console 自己的 cookie。
  response.cookies.set("saas_access_token", token.access_token, { ...common, httpOnly: true });
  // SSO 橋:/ui 與 console 用**同一顆 JWT**(同 secret_key 簽)。登入 console 時
  // 一併種下 /ui 的 auth + double-submit CSRF cookie(規格同 ui.py:_set_auth_cookie),
  // 使用者切到舊版後台免二次登入;/ui 登入亦會回種 saas_access_token。
  response.cookies.set("access_token", token.access_token, { ...common, httpOnly: true });
  response.cookies.set("csrf_token", randomToken(), { ...common, httpOnly: false });
  return response;
}
