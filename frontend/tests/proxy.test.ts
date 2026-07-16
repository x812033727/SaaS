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
