import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { GET as proxyGet, POST as proxyPost } from "@/app/api/proxy/[...path]/route";

function req(
  path: string[],
  {
    method = "GET",
    token = "jwt-abc",
    origin,
  }: { method?: string; token?: string | null; origin?: string } = {},
) {
  const headers: Record<string, string> = { host: "console.local" };
  if (origin) headers.origin = origin;
  if (token) headers.cookie = `saas_access_token=${token}`;
  const request = new NextRequest(`http://console.local/console/api/proxy/${path.join("/")}`, {
    method,
    headers,
  });
  return { request, ctx: { params: Promise.resolve({ path }) } };
}

afterEach(() => vi.unstubAllGlobals());

describe("proxy route", () => {
  it("白名單外路徑 404、不外呼", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["admin", "secrets"]);
    const response = await proxyGet(request, ctx);
    expect(response.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("無 cookie 401", async () => {
    const { request, ctx } = req(["api", "v1", "reservations"], { token: null });
    expect((await proxyGet(request, ctx)).status).toBe(401);
  });

  it("轉發白名單路徑並帶 Bearer + 傳回 X-Total-Count", async () => {
    const fetchSpy = vi.fn(
      async () =>
        new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json", "x-total-count": "7" },
        }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["api", "v1", "reservations"]);
    const response = await proxyGet(request, ctx);
    expect(response.status).toBe(200);
    expect(response.headers.get("X-Total-Count")).toBe("7");
    const [url, init] = fetchSpy.mock.calls[0] as unknown as [URL, RequestInit];
    expect(String(url)).toContain("/api/v1/reservations");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer jwt-abc");
  });

  it("mutating 請求 Origin 與 Host 不符 → 403", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["booking", "reservations"], {
      method: "POST",
      origin: "https://evil.example",
    });
    const response = await proxyPost(request, ctx);
    expect(response.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("mutating 請求同源 Origin 放行", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 201, headers: { "content-type": "application/json" } })),
    );
    const { request, ctx } = req(["booking", "reservations"], {
      method: "POST",
      origin: "http://console.local",
    });
    expect((await proxyPost(request, ctx)).status).toBe(201);
  });
});

// ── R4-C1:滑動續期 ──────────────────────────────────────────────────────────

function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "HS256" })}.${b64(payload)}.sig`;
}

describe("proxy sliding renewal", () => {
  it("快過期 token → 先 renew、轉發用新 token、回應種新 cookie 且 csrf 沿用", async () => {
    const near = fakeJwt({ exp: Math.floor(Date.now() / 1000) + 600 });
    const fetchSpy = vi.fn(async (url: URL | string) => {
      if (String(url).includes("/auth/renew")) {
        return new Response(JSON.stringify({ access_token: "renewed-jwt" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["api", "v1", "reservations"], { token: near });
    request.cookies.set("csrf_token", "old-csrf");
    const response = await proxyGet(request, ctx);
    expect(response.status).toBe(200);
    const calls = fetchSpy.mock.calls.map((c) => String(c[0]));
    expect(calls[0]).toContain("/auth/renew");
    const forward = fetchSpy.mock.calls[1] as unknown as [URL, RequestInit];
    expect((forward[1].headers as Record<string, string>).Authorization).toBe("Bearer renewed-jwt");
    const setCookies = response.headers.getSetCookie();
    expect(setCookies.some((c) => c.startsWith("saas_access_token=renewed-jwt"))).toBe(true);
    expect(setCookies.some((c) => c.startsWith("access_token=renewed-jwt"))).toBe(true);
    expect(setCookies.some((c) => c.startsWith("csrf_token=old-csrf"))).toBe(true);
  });

  it("新鮮 token 不打 renew", async () => {
    const fresh = fakeJwt({ exp: Math.floor(Date.now() / 1000) + 3500 });
    const fetchSpy = vi.fn(async () => new Response("[]", {
      status: 200, headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["api", "v1", "reservations"], { token: fresh });
    const response = await proxyGet(request, ctx);
    expect(response.status).toBe(200);
    expect((fetchSpy.mock.calls as unknown[][]).every((c) => !String(c[0]).includes("/auth/renew"))).toBe(true);
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("imp 代管票不續", async () => {
    const imp = fakeJwt({ exp: Math.floor(Date.now() / 1000) + 600, imp: 9 });
    const fetchSpy = vi.fn(async () => new Response("[]", {
      status: 200, headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["api", "v1", "reservations"], { token: imp });
    await proxyGet(request, ctx);
    expect((fetchSpy.mock.calls as unknown[][]).every((c) => !String(c[0]).includes("/auth/renew"))).toBe(true);
  });

  it("renew 失敗不阻斷原請求(沿用舊 token)", async () => {
    const near = fakeJwt({ exp: Math.floor(Date.now() / 1000) + 600 });
    const fetchSpy = vi.fn(async (url: URL | string) => {
      if (String(url).includes("/auth/renew")) return new Response("no", { status: 500 });
      return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchSpy);
    const { request, ctx } = req(["api", "v1", "reservations"], { token: near });
    const response = await proxyGet(request, ctx);
    expect(response.status).toBe(200);
    const forward = fetchSpy.mock.calls[1] as unknown as [URL, RequestInit];
    expect((forward[1].headers as Record<string, string>).Authorization).toBe(`Bearer ${near}`);
  });
});
