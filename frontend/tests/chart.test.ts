import { describe, expect, it } from "vitest";

import { niceMax, normalize, ticks } from "@/lib/chart";

describe("chart helpers", () => {
  it("niceMax 取好看的上限", () => {
    expect(niceMax([0])).toBe(1);
    expect(niceMax([7])).toBe(10);
    expect(niceMax([12])).toBe(20);
    expect(niceMax([23])).toBe(25);
    expect(niceMax([40, 3])).toBe(50);
    expect(niceMax([100])).toBe(100);
  });
  it("normalize 相對 niceMax 落在 0..1", () => {
    const out = normalize([5, 10]);
    expect(out[1]).toBe(1);
    expect(out[0]).toBe(0.5);
  });
  it("ticks 等分", () => {
    expect(ticks(100, 4)).toEqual([0, 25, 50, 75, 100]);
  });
});
