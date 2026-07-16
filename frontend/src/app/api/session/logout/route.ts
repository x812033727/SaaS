import { NextResponse } from "next/server";

export async function POST(request: Request) {
  // 任一邊登出都清三個 cookie(console + /ui auth + CSRF),防雙 cookie 漂移。
  const response = NextResponse.redirect(new URL("/console/login", request.url), { status: 303 });
  for (const name of ["saas_access_token", "access_token", "csrf_token"]) {
    response.cookies.set(name, "", { path: "/", maxAge: 0 });
  }
  return response;
}
