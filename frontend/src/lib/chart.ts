/** 純函式圖表刻度/正規化 helpers(vitest 覆蓋;SVG 元件只做渲染)。 */

/** 取「好看」的軸上限:1/2/2.5/5×10^n 中首個 ≥ max 者;全零回 1。 */
export function niceMax(values: number[]): number {
  const max = Math.max(0, ...values);
  if (max === 0) return 1;
  const exp = Math.floor(Math.log10(max));
  for (const m of [1, 2, 2.5, 5, 10]) {
    const candidate = m * 10 ** exp;
    if (candidate >= max) return candidate;
  }
  return 10 ** (exp + 1);
}

/** 把值正規化為 0..1(相對 niceMax)。 */
export function normalize(values: number[]): number[] {
  const top = niceMax(values);
  return values.map((v) => Math.max(0, v) / top);
}

/** 產生 n 個等分刻度標籤(0 → top)。 */
export function ticks(top: number, count = 4): number[] {
  return Array.from({ length: count + 1 }, (_, i) => Math.round((top / count) * i));
}
