/**
 * Console 靜態守門(R10)— 把兩類「CI 抓不到、上線才死」的缺陷固化成測試:
 *
 * 1. R7-C4:console 頁面對 proxy 的 API 呼叫必須是完整後端路徑
 *    (proxy allowlist 慣例);裸路徑會被 isAllowed 判 404,頁面整頁死。
 * 2. R8-4:raw <a href="/..."> 不吃 Next basePath(/console),
 *    會落到 FastAPI 404;內部連結必須帶 /console 前綴(或指向後端
 *    /ui、/auth 等刻意的 server 端路徑)。
 *
 * 兩者皆從 proxy route 原始碼「同步」解析 allowlist/denylist,
 * 清單改了測試自動跟上,不會漂移。
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "src");
const PROXY_ROUTE = join(SRC, "app", "api", "proxy", "[...path]", "route.ts");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return walk(p);
    return /\.(tsx|ts)$/.test(name) ? [p] : [];
  });
}

const PAGE_FILES = walk(join(SRC, "app", "(app)")).concat(
  walk(join(SRC, "components")),
);

function parseProxyLists(): { allowed: string[]; denied: string[] } {
  const src = readFileSync(PROXY_ROUTE, "utf-8");
  const allowedMatch = src.match(/ALLOWED_PREFIXES\s*=\s*\[([^\]]+)\]/);
  const deniedMatch = src.match(/DENIED_PATHS\s*=\s*new Set\(\[([^\]]+)\]\)/);
  const pull = (blob: string | undefined) =>
    blob ? [...blob.matchAll(/"([^"]+)"/g)].map((m) => m[1]) : [];
  return { allowed: pull(allowedMatch?.[1]), denied: pull(deniedMatch?.[1]) };
}

const { allowed, denied } = parseProxyLists();

it("proxy 清單解析成功(防守門自身失效)", () => {
  expect(allowed.length).toBeGreaterThanOrEqual(4);
  expect(allowed).toContain("api/v1/");
  expect(denied.length).toBeGreaterThanOrEqual(2);
});

// 與 proxy isAllowed 同語義
function isAllowed(path: string): boolean {
  if (denied.includes(path)) return false;
  return allowed.some((p) => path === p.slice(0, -1) || path.startsWith(p));
}

describe("API 呼叫路徑必過 proxy allowlist(R7-C4 缺陷類)", () => {
  // fetchJson<T>("/x")、postJson(`/x/${id}`) 等;模板字串取第一個 ${ 前的靜態前綴
  const CALL_RE =
    /(?:fetchJson|fetchList|postJson|patchJson|putJson|delJson)(?:<[^>]*>)?\(\s*(?:"([^"]+)"|`([^`$]+)(?:\$\{)?)/g;

  for (const file of PAGE_FILES) {
    const src = readFileSync(file, "utf-8");
    const calls = [...src.matchAll(CALL_RE)]
      .map((m) => m[1] ?? m[2])
      .filter(Boolean);
    if (calls.length === 0) continue;
    it(file.slice(SRC.length + 1), () => {
      for (const raw of calls) {
        const path = raw.replace(/^\//, "").split("?")[0];
        expect(
          isAllowed(path),
          `「${raw}」不在 proxy allowlist(或被 DENIED_PATHS 拒),上線會 404`,
        ).toBe(true);
      }
    });
  }
});

describe("raw href 內部連結必帶 basePath 或指向刻意的後端路徑(R8-4 缺陷類)", () => {
  // 合法:/console/...(basePath 顯式)、/ui/...(後端 Jinja)、/auth/...(OAuth 等
  // server 端流)、/p/...(FastAPI 公開店家頁)。其餘裸路徑會繞過 basePath 404。
  const OK = [/^\/console\//, /^\/ui(\/|$)/, /^\/auth\//, /^\/p\//];
  const HREF_RE = /href=(?:"(\/[^"]*)"|\{`(\/[^`$]*))/g;

  for (const file of PAGE_FILES) {
    const src = readFileSync(file, "utf-8");
    // next/link 的 Link 元件會自動加 basePath,不在本守門範圍:
    // 只掃 raw <a href;Link 使用處以 <Link href 出現,先移除再掃。
    const withoutLink = src.replace(/<Link\s[^>]*href=/g, "<Link data-checked=");
    const hrefs = [...withoutLink.matchAll(HREF_RE)]
      .map((m) => m[1] ?? m[2])
      .filter(Boolean);
    if (hrefs.length === 0) continue;
    it(file.slice(SRC.length + 1), () => {
      for (const href of hrefs) {
        expect(
          OK.some((re) => re.test(href)),
          `raw <a href="${href}"> 會繞過 Next basePath 落到 FastAPI 404;` +
            "請改 /console/... 前綴或 next/link",
        ).toBe(true);
      }
    });
  }
});
