import { NextRequest, NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const tokenMaxAgeSeconds =
  Number(process.env.SAAS_ACCESS_TOKEN_EXPIRE_MINUTES ?? "60") * 60;

const secure = process.env.NODE_ENV === "production";

// token_version+1 類操作(改密碼/登出所有裝置)會撤銷含當下這顆票;
// 此 route 以現有 cookie 打對應後端端點,拿回新票後重種 cookie
// (規格同 /api/session/login),操作者本裝置不掉線。
// 白名單限定兩個端點,不做通用轉發;這兩個端點同時被 proxy 的
// DENIED_PATHS 擋下,確保回傳的 token 永不落入瀏覽器 JS。
const ALLOWED: Record<string, string> = {
  password: "/api/v1/account/password",
  "logout-all": "/api/v1/account/logout-all",
};

function sameOrigin(request: NextRequest): boolean {
  // 與 proxy 相同的第二層 CSRF 防禦:mutating 請求 Origin 必須等於 Host。
  const origin = request.headers.get("origin");
  if (!origin) return true;
  const host = request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) {
    return NextResponse.json({ detail: "bad_origin" }, { status: 403 });
  }
  let action: string | undefined;
  let body: Record<string, unknown> | undefined;
  try {
    ({ action, body } = (await request.json()) as {
      action?: string;
      body?: Record<string, unknown>;
    });
  } catch {
    return NextResponse.json({ detail: "invalid_request" }, { status: 400 });
  }
  const path = action ? ALLOWED[action] : undefined;
  if (!path) return NextResponse.json({ detail: "invalid_action" }, { status: 400 });

  const token = request.cookies.get("saas_access_token")?.value;
  if (!token) return NextResponse.json({ detail: "unauthenticated" }, { status: 401 });

  // 轉發真實客戶端 IP(auth.logout_all 稽核的 IP 有鑑識價值,比照 login route)。
  const forwardedFor = request.headers.get("x-forwarded-for");
  const upstream = await fetch(`${apiOrigin}${path}`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(forwardedFor ? { "x-forwarded-for": forwardedFor } : {}),
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
  // SSO 橋:/ui 同一顆 JWT。csrf 沿用舊值只延壽命——輪替會讓已開啟的 /ui
  // 分頁上既有表單的 double-submit CSRF 不符(比照 /ui 改密路由的作法)。
  response.cookies.set("access_token", data.access_token, { ...common, httpOnly: true });
  const csrf = request.cookies.get("csrf_token")?.value;
  if (csrf) response.cookies.set("csrf_token", csrf, { ...common, httpOnly: false });
  return response;
}
