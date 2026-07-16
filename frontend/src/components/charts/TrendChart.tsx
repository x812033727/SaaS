import { niceMax } from "@/lib/chart";

type Point = { label: string; bookings: number; revenueTwd: number };

/** 雙序列趨勢圖:預約量=品牌綠長條、營收=琥珀金折線。純 SVG 零依賴,
 * 配色走 @theme token(currentColor/CSS 變數)。 */
export default function TrendChart({ points }: { points: Point[] }) {
  const W = 720;
  const H = 220;
  const PAD = { top: 12, right: 44, bottom: 28, left: 44 };
  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;

  const bookingTop = niceMax(points.map((p) => p.bookings));
  const revenueTop = niceMax(points.map((p) => p.revenueTwd));
  const band = iw / Math.max(1, points.length);
  const barW = Math.min(28, band * 0.55);

  const x = (i: number) => PAD.left + band * i + band / 2;
  const yBooking = (v: number) => PAD.top + ih * (1 - v / bookingTop);
  const yRevenue = (v: number) => PAD.top + ih * (1 - v / revenueTop);

  const linePath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${yRevenue(p.revenueTwd).toFixed(1)}`)
    .join(" ");

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="預約與營收趨勢">
      {/* 格線 */}
      {[0, 0.5, 1].map((f) => (
        <line key={f}
          x1={PAD.left} x2={W - PAD.right}
          y1={PAD.top + ih * f} y2={PAD.top + ih * f}
          stroke="var(--color-line)" strokeWidth={1}
        />
      ))}
      {/* 左軸(預約)/右軸(營收)標籤 */}
      <text x={PAD.left - 6} y={PAD.top + 4} textAnchor="end" fontSize={10} fill="var(--color-muted)">
        {bookingTop}
      </text>
      <text x={W - PAD.right + 6} y={PAD.top + 4} fontSize={10} fill="var(--color-gold)">
        {revenueTop >= 10000 ? `${Math.round(revenueTop / 1000)}k` : revenueTop}
      </text>
      {/* 預約長條 */}
      {points.map((p, i) => (
        <rect key={p.label}
          x={x(i) - barW / 2}
          y={yBooking(p.bookings)}
          width={barW}
          height={PAD.top + ih - yBooking(p.bookings)}
          rx={3}
          fill="var(--color-brand)"
          opacity={0.85}
        />
      ))}
      {/* 營收折線 */}
      <path d={linePath} fill="none" stroke="var(--color-gold)" strokeWidth={2} />
      {points.map((p, i) => (
        <circle key={p.label} cx={x(i)} cy={yRevenue(p.revenueTwd)} r={2.5} fill="var(--color-gold)" />
      ))}
      {/* X 標籤(疏化:最多 6 個) */}
      {points.map((p, i) => {
        const step = Math.ceil(points.length / 6);
        if (i % step !== 0 && i !== points.length - 1) return null;
        return (
          <text key={p.label} x={x(i)} y={H - 8} textAnchor="middle" fontSize={10} fill="var(--color-muted)">
            {p.label}
          </text>
        );
      })}
    </svg>
  );
}
