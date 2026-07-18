"use client";

/** client 端 API helper:一律走 /console/api/proxy(JWT 留在 httpOnly cookie)。
 * 401 導回登入;回傳含 X-Total-Count 的列表用 fetchList。 */

const PROXY = "/console/api/proxy";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`API_${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let detail = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? "");
  } catch {
    detail = response.statusText;
  }
  return new ApiError(response.status, detail);
}

export async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${PROXY}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...init.headers },
  });
  if (response.status === 401) {
    window.location.href = "/console/login";
    throw new ApiError(401, "unauthenticated");
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function fetchList<T>(
  path: string,
  init: RequestInit = {},
): Promise<{ rows: T[]; total: number }> {
  const response = await fetch(`${PROXY}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...init.headers },
  });
  if (response.status === 401) {
    window.location.href = "/console/login";
    throw new ApiError(401, "unauthenticated");
  }
  if (!response.ok) throw await parseError(response);
  const rows = (await response.json()) as T[];
  const total = Number(response.headers.get("X-Total-Count") ?? rows.length);
  return { rows, total };
}

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return fetchJson<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function patchJson<T>(path: string, body: unknown): Promise<T> {
  return fetchJson<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function putJson<T>(path: string, body: unknown): Promise<T> {
  return fetchJson<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function delJson<T>(path: string): Promise<T> {
  return fetchJson<T>(path, { method: "DELETE" });
}
