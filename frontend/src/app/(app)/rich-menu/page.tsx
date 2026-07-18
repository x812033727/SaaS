"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type Options = {
  themes: string[];
  templates: string[];
  template_labels?: Record<string, string>;
};

type Status = {
  applied: boolean;
  rich_menu_id: string | null;
  template: string | null;
  theme: string | null;
};

const THEME_LABELS: Record<string, string> = {
  line_green: "LINE 綠", ocean_blue: "海洋藍", royal_purple: "皇家紫",
  sunset_orange: "夕陽橘", dark: "深色", rose_pink: "玫瑰粉",
  boutique: "精品香檳金", brand: "品牌墨綠",
};

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

export default function RichMenuPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [template, setTemplate] = useState("booking4");
  const [theme, setTheme] = useState("brand");
  // custom_image 模式:店家自備背景圖(base64 上傳;按鈕分區仍取決於版型)
  const [mode, setMode] = useState<"template" | "custom_image">("template");
  const [imageBase64, setImageBase64] = useState<string | null>(null);
  const [imagePreview, setImagePreview] = useState<string | null>(null);

  function onImageFile(file: File | null) {
    if (!file) { setImageBase64(null); setImagePreview(null); return; }
    if (file.size > 1024 * 1024) {
      setMsg({ kind: "error", text: "背景圖請小於 1MB(LINE 上限;建議 2500×1686 PNG/JPEG)。" });
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      setImagePreview(dataUrl);
      setImageBase64(dataUrl.split(",", 2)[1] ?? null);
      setMsg(null);
    };
    reader.readAsDataURL(file);
  }

  const options = useQuery({
    queryKey: ["rich-menu-options"],
    queryFn: () => fetchJson<Options>("/booking/rich-menu/options"),
    retry: false,
  });
  const status = useQuery({
    queryKey: ["rich-menu-status"],
    queryFn: () => fetchJson<Status>("/booking/rich-menu/status"),
    retry: false,
  });

  const apply = useMutation({
    mutationFn: () =>
      postJson<Status>("/booking/rich-menu/apply", {
        template,
        theme,
        mode,
        ...(mode === "custom_image" ? { image_base64: imageBase64 } : {}),
      }),
    onSuccess: () => {
      setMsg({ kind: "ok", text: "圖文選單已套用到 LINE 官方帳號。" });
      qc.invalidateQueries({ queryKey: ["rich-menu-status"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });
  const clear = useMutation({
    mutationFn: () => postJson<Status>("/booking/rich-menu/clear", {}),
    onSuccess: () => {
      setMsg({ kind: "ok", text: "圖文選單已移除。" });
      qc.invalidateQueries({ queryKey: ["rich-menu-status"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  // 404 = 尚未設定 LINE(鏡射 /ui 的 not-configured 空狀態)
  const notConfigured = status.error instanceof ApiError && status.error.status === 404;
  const st = status.data;

  const previewSrc =
    `/console/api/proxy/booking/rich-menu/preview.png?template=${encodeURIComponent(template)}&theme=${encodeURIComponent(theme)}`;

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">LINE 圖文選單</h1>
      <p className="mt-1 text-sm text-muted">
        顧客對話視窗底部的固定選單;選擇版型與主題色後套用,即時生效於官方帳號。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      {notConfigured && (
        <div className="mt-6 rounded-xl border border-line bg-warn-soft p-6 text-sm">
          <p className="font-semibold text-warn">尚未設定 LINE 官方帳號</p>
          <p className="mt-2 text-ink">
            請先至<a href="/console/line-settings" className="mx-1 text-brand underline">LINE 設定</a>
            完成 Channel 憑證設定,再回來套用圖文選單。
          </p>
        </div>
      )}

      {st && (
        <div className={`mt-6 rounded-xl border p-4 text-sm ${st.applied ? "border-ok/40 bg-ok-soft" : "border-line bg-surface"}`}>
          {st.applied ? (
            <p className="text-ok">
              目前已套用:{options.data?.template_labels?.[st.template ?? ""] ?? st.template}・
              {THEME_LABELS[st.theme ?? ""] ?? st.theme}
            </p>
          ) : (
            <p className="text-muted">目前未套用圖文選單。</p>
          )}
        </div>
      )}

      {!notConfigured && options.data && (
        <section className="mt-4 rounded-xl border border-line bg-surface p-4">
          <h2 className="font-semibold">套用選單</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="text-sm">
              版型
              <select value={template} onChange={(e) => setTemplate(e.target.value)} className={inputCls}>
                {options.data.templates.map((t) => (
                  <option key={t} value={t}>{options.data!.template_labels?.[t] ?? t}</option>
                ))}
              </select>
            </label>
            <label className="text-sm">
              主題色
              <select value={theme} onChange={(e) => setTheme(e.target.value)} className={inputCls}>
                {options.data.themes.map((t) => (
                  <option key={t} value={t}>{THEME_LABELS[t] ?? t}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-3 text-sm">
            <label>背景模式
              <select value={mode}
                onChange={(e) => setMode(e.target.value as "template" | "custom_image")}
                className={inputCls}>
                <option value="template">主題模板(純色背景)</option>
                <option value="custom_image">自訂背景圖</option>
              </select>
            </label>
            {mode === "custom_image" && (
              <label>背景圖(PNG/JPEG,建議 2500×1686,&lt;1MB)
                <input type="file" accept="image/png,image/jpeg"
                  onChange={(e) => onImageFile(e.target.files?.[0] ?? null)}
                  className="mt-1 block text-sm" />
              </label>
            )}
          </div>
          <div className="mt-4 overflow-hidden rounded-lg border border-line/60">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={mode === "custom_image" && imagePreview ? imagePreview : previewSrc}
              alt="選單預覽" className="w-full" />
          </div>
          {mode === "custom_image" && !imagePreview && (
            <p className="mt-1 text-xs text-muted">尚未選擇背景圖;上方顯示的是模板預覽,按鈕分區依所選版型。</p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              disabled={apply.isPending || (mode === "custom_image" && !imageBase64)}
              className={btnCls} onClick={() => apply.mutate()}>
              {apply.isPending ? "套用中…" : "套用到 LINE"}
            </button>
            {st?.applied && (
              <button disabled={clear.isPending}
                className="rounded-lg border border-line px-4 py-2 text-sm text-danger hover:bg-danger-soft"
                onClick={() => { if (confirm("移除官方帳號的圖文選單?")) clear.mutate(); }}>
                移除選單
              </button>
            )}
          </div>
          <p className="mt-2 text-xs text-muted">
            套用需呼叫 LINE API(建立選單+上傳圖+設為預設),失敗時不會變更現有狀態。
          </p>
        </section>
      )}
    </div>
  );
}
