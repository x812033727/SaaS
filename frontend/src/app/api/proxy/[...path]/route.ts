import { NextRequest, NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

// 只轉發 console 需要的 API 前綴;JWT 永遠留在 httpOnly cookie,瀏覽器不接觸。
const ALLOWED_PREFIXES = ["api/v1/", "booking/"];

const MUTATING = new Set(["POST", "PATCH", "PUT", "DELETE"]);

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
  const token = request.cookies.get("saas_access_token")?.value;
  if (!token) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

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
  return new NextResponse(await upstream.arrayBuffer(), { status: upstream.status, headers });
}

export { handle as GET, handle as POST, handle as PATCH, handle as PUT, handle as DELETE };
