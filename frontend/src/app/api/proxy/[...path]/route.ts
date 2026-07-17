import { NextRequest, NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

// 只轉發 console 需要的 API 前綴;JWT 永遠留在 httpOnly cookie,瀏覽器不接觸。
// tenants/me 是**窄前綴**(自助 LINE 設定),不開整個 tenants/(避免暴露列表/管理端點)。
// ai/faq 同為窄前綴(R5-A4 FAQ 頁):刻意不開 ai/ask(付費 LLM,成本敏感,留 /ui)。
// notes/ 與 api-keys/ 為 console 頁窄前綴(R6-D3);ai/ask 仍刻意不開(付費 LLM)。
const ALLOWED_PREFIXES = ["api/v1/", "booking/", "tenants/me", "ai/faq", "notes/", "api-keys/"];

const MUTATING = new Set(["POST", "PATCH", "PUT", "DELETE"]);

// 滑動續期(R4-C1):token 剩餘壽命低於門檻時,經由後端 /auth/renew 換新。
// 只讀 payload 不驗簽(驗簽是後端 /auth/renew 的職責);imp 代管票不續。
const RENEW_THRESHOLD_SECONDS =
  Number(process.env.SAAS_SESSION_RENEW_THRESHOLD_MINUTES ?? "30") * 60;
const tokenMaxAgeSeconds =
  Number(process.env.SAAS_ACCESS_TOKEN_EXPIRE_MINUTES ?? "60") * 60;

function decodeJwtPayload(token: string): { exp?: number; imp?: unknown } | null {
  try {
    const part = token.split(".")[1];
    return JSON.parse(Buffer.from(part, "base64url").toString("utf-8"));
  } catch {
    return null;
  }
}

async function maybeRenewToken(token: string): Promise<string | null> {
  const payload = decodeJwtPayload(token);
  if (!payload?.exp || payload.imp != null) return null;
  const remaining = payload.exp - Math.floor(Date.now() / 1000);
  if (remaining <= 0 || remaining > RENEW_THRESHOLD_SECONDS) return null;
  try {
    const upstream = await fetch(`${apiOrigin}/auth/renew`, {
      method: "POST",
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!upstream.ok) return null;
    const body = (await upstream.json()) as { access_token?: string };
    return body.access_token ?? null;
  } catch {
    return null; // 續期失敗絕不阻斷原請求
  }
}

function applyRenewedCookies(response: NextResponse, request: NextRequest, token: string): void {
  const secure = process.env.NODE_ENV === "production";
  const common = { secure, sameSite: "lax" as const, path: "/", maxAge: tokenMaxAgeSeconds };
  response.cookies.set("saas_access_token", token, { ...common, httpOnly: true });
  response.cookies.set("access_token", token, { ...common, httpOnly: true });
  // csrf 沿用舊值只延壽命(輪替會壞 /ui 已渲染表單的 double-submit)。
  const csrf = request.cookies.get("csrf_token")?.value;
  if (csrf) response.cookies.set("csrf_token", csrf, { ...common, httpOnly: false });
}

function isAllowed(path: string): boolean {
  return ALLOWED_PREFIXES.some((p) => path === p.slice(0, -1) || path.startsWith(p));
}

function sameOrigin(request: NextRequest): boolean {
  // SameSite=Lax 已擋跨站 POST;此為第二層:mutating 請求的 Origin 必須與 Host 一致。
  const origin = request.headers.get("origin");
  if (!origin) return true; // 同源導覽/非瀏覽器情境無 Origin
  const host = request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

async function handle(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const segments = (await params).path;
  const path = segments.join("/");
  if (!isAllowed(path)) {
    return NextResponse.json({ error: "not_allowed" }, { status: 404 });
  }
  if (MUTATING.has(request.method) && !sameOrigin(request)) {
    return NextResponse.json({ error: "bad_origin" }, { status: 403 });
  }
  let token = request.cookies.get("saas_access_token")?.value;
  if (!token) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }
  const renewed = await maybeRenewToken(token);
  if (renewed) token = renewed;

  const url = new URL(`${apiOrigin}/${path}`);
  url.search = request.nextUrl.search;
  const upstream = await fetch(url, {
    method: request.method,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      ...(request.headers.get("content-type")
        ? { "Content-Type": request.headers.get("content-type") as string }
        : {}),
    },
    body: MUTATING.has(request.method) ? await request.arrayBuffer() : undefined,
  });

  const headers = new Headers({ "Content-Type": upstream.headers.get("content-type") ?? "application/json" });
  const totalCount = upstream.headers.get("x-total-count");
  if (totalCount) headers.set("X-Total-Count", totalCount);
  const response = new NextResponse(await upstream.arrayBuffer(), { status: upstream.status, headers });
  if (renewed) applyRenewedCookies(response, request, renewed);
  return response;
}

export { handle as GET, handle as POST, handle as PATCH, handle as PUT, handle as DELETE };
