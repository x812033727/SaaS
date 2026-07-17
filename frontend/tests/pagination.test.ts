import { describe, expect, it } from "vitest";

import { clampPage, pageCount, pageOffset } from "@/lib/pagination";

describe("pageCount", () => {
  it("ceils total/pageSize, at least 1", () => {
    expect(pageCount(0, 30)).toBe(1);
    expect(pageCount(1, 30)).toBe(1);
    expect(pageCount(30, 30)).toBe(1);
    expect(pageCount(31, 30)).toBe(2);
    expect(pageCount(90, 30)).toBe(3);
  });
  it("guards zero/negative pageSize and negative total", () => {
    expect(pageCount(100, 0)).toBe(1);
    expect(pageCount(-5, 30)).toBe(1);
  });
});

describe("clampPage", () => {
  it("clamps into [0, totalPages-1]", () => {
    expect(clampPage(-1, 3)).toBe(0);
    expect(clampPage(0, 3)).toBe(0);
    expect(clampPage(2, 3)).toBe(2);
    expect(clampPage(5, 3)).toBe(2); // total shrank → last page
    expect(clampPage(0, 1)).toBe(0);
  });
});

describe("pageOffset", () => {
  it("is page*pageSize, floored at 0", () => {
    expect(pageOffset(0, 30)).toBe(0);
    expect(pageOffset(2, 30)).toBe(60);
    expect(pageOffset(-1, 30)).toBe(0);
  });
});
