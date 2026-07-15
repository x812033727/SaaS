import { NextResponse } from "next/server";

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

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
  response.cookies.set("saas_access_token", token.access_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 8,
  });
  return response;
}
