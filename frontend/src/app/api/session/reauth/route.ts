import { NextRequest, NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const tokenMaxAgeSeconds =
  Number(process.env.SAAS_ACCESS_TOKEN_EXPIRE_MINUTES ?? "60") * 60;

const secure = process.env.NODE_ENV === "production";

// token_version+1 類操作(改密碼/登出所有裝置)會撤銷含當下這顆票;
// 此 route 以現有 cookie 打對應後端端點,拿回新票後重種三顆 cookie
// (規格同 /api/session/login),操作者本裝置不掉線。
// 白名單限定兩個端點,不做通用轉發。
const ALLOWED: Record<string, string> = {
  password: "/api/v1/account/password",
  "logout-all": "/api/v1/account/logout-all",
};

function randomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Buffer.from(bytes).toString("base64url");
}

export async function POST(request: NextRequest) {
  const { action, body } = (await request.json()) as {
    action?: string;
    body?: Record<string, unknown>;
  };
  const path = action ? ALLOWED[action] : undefined;
  if (!path) return NextResponse.json({ error: "invalid_action" }, { status: 400 });

  const token = request.cookies.get("saas_access_token")?.value;
  if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

  const upstream = await fetch(`${apiOrigin}${path}`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body ?? {}),
  });
  if (!upstream.ok) {
    let detail = "";
    try {
      detail = ((await upstream.json()) as { detail?: string }).detail ?? "";
    } catch {
      detail = upstream.statusText;
    }
    return NextResponse.json({ detail }, { status: upstream.status });
  }
  const data = (await upstream.json()) as { access_token: string };
  const response = NextResponse.json({ ok: true });
  const common = { secure, sameSite: "lax" as const, path: "/", maxAge: tokenMaxAgeSeconds };
  response.cookies.set("saas_access_token", data.access_token, { ...common, httpOnly: true });
  // SSO 橋:/ui 同一顆 JWT + double-submit CSRF(規格同 /api/session/login)。
  response.cookies.set("access_token", data.access_token, { ...common, httpOnly: true });
  response.cookies.set("csrf_token", randomToken(), { ...common, httpOnly: false });
  return response;
}
