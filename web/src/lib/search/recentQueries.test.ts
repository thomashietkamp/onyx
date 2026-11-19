import { recentQueries } from "@/lib/search/utils";

describe("recentQueries", () => {
  it("keeps the newest distinct queries and drops the current one", () => {
    expect(
      recentQueries(
        ["Budget 2026", "budget 2026 ", "onboarding", "Budget 2026", "vpn", ""],
        "ONBOARDING",
        5
      )
    ).toEqual(["Budget 2026", "vpn"]);
  });

  it("stops at the limit", () => {
    expect(recentQueries(["a", "b", "c"], null, 2)).toEqual(["a", "b"]);
  });
});
