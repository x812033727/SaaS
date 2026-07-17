import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as loginPost } from "@/app/api/session/login/route";

function loginRequest(body: unknown, extraHeaders: Record<string, string> = {}): Request {
  return new Request("http://console.local/console/api/session/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extraHeaders },
    body: JSON.stringify(body),
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("session login route", () => {
  it("成功登入種齊三個 cookie(console + /ui SSO 橋),maxAge 對齊 JWT 60 分", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ access_token: "jwt-abc" }), { status: 200 })),
    );
    const response = await loginPost(loginRequest({ email: "o@x.tw", password: "pw" }));
    expect(response.status).toBe(200);
    const setCookies = response.headers.getSetCookie();
    const byName = Object.fromEntries(setCookies.map((c) => [c.split("=")[0], c]));
    expect(Object.keys(byName).sort()).toEqual(["access_token", "csrf_token", "saas_access_token"]);
    expect(byName["saas_access_token"]).toContain("jwt-abc");
    expect(byName["saas_access_token"]).toContain("HttpOnly");
    expect(byName["saas_access_token"]).toContain("Max-Age=3600");
    expect(byName["access_token"]).toContain("jwt-abc");
    expect(byName["access_token"]).toContain("HttpOnly");
    // double-submit CSRF cookie 必須可被前端讀取(非 HttpOnly)
    expect(byName["csrf_token"]).not.toContain("HttpOnly");
  });

  it("憑證錯誤回 401 且不種 cookie", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 401 })));
    const response = await loginPost(loginRequest({ email: "o@x.tw", password: "bad" }));
    expect(response.status).toBe(401);
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("缺欄位回 400", async () => {
    const response = await loginPost(loginRequest({ email: "" }));
    expect(response.status).toBe(400);
  });

  it("轉發 X-Forwarded-For 給後端(登入稽核要真實客戶端 IP)", async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(JSON.stringify({ access_token: "jwt-abc" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await loginPost(
      loginRequest({ email: "o@x.tw", password: "pw" }, { "x-forwarded-for": "203.0.113.9" }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headers["x-forwarded-for"]).toBe("203.0.113.9");
  });

  it("無 X-Forwarded-For 時不憑空加 header", async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(JSON.stringify({ access_token: "jwt-abc" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await loginPost(loginRequest({ email: "o@x.tw", password: "pw" }));
    const headers = fetchMock.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headers["x-forwarded-for"]).toBeUndefined();
  });
});
