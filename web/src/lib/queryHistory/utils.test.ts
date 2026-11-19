import {
  chatSessionHistoryUrl,
  downloadExportUrl,
  hasPendingExport,
  previewText,
  startExportUrl,
  toFeedbackType,
} from "@/lib/queryHistory/utils";
import type { QueryHistoryExport } from "@/lib/queryHistory/types";

function exportWith(status: QueryHistoryExport["status"]): QueryHistoryExport {
  return {
    request_id: status,
    status,
    start: null,
    end: null,
    time_requested: "2026-01-01T00:00:00Z",
  };
}

describe("toFeedbackType", () => {
  test("keeps known feedback types", () => {
    expect(toFeedbackType("like")).toBe("like");
    expect(toFeedbackType("dislike")).toBe("dislike");
    expect(toFeedbackType("mixed")).toBe("mixed");
  });

  test("treats any other value as no filter", () => {
    expect(toFeedbackType("any")).toBeNull();
    expect(toFeedbackType("")).toBeNull();
  });
});

describe("chatSessionHistoryUrl", () => {
  test("sends only the filters that are set", () => {
    expect(
      chatSessionHistoryUrl({
        feedbackType: null,
        start: null,
        end: null,
        pageNum: 2,
        pageSize: 25,
      })
    ).toBe("/api/admin/chat-session-history?page_num=2&page_size=25");
  });

  test("encodes the feedback type and the range as ISO times", () => {
    const url = chatSessionHistoryUrl({
      feedbackType: "mixed",
      start: new Date("2026-01-01T00:00:00Z"),
      end: new Date("2026-01-31T23:59:59.999Z"),
      pageNum: 0,
      pageSize: 10,
    });
    const params = new URL(url, "http://localhost").searchParams;
    expect(params.get("feedback_type")).toBe("mixed");
    expect(params.get("start_time")).toBe("2026-01-01T00:00:00.000Z");
    expect(params.get("end_time")).toBe("2026-01-31T23:59:59.999Z");
  });
});

describe("export URLs", () => {
  test("start-export omits an open range end", () => {
    expect(startExportUrl(new Date("2026-01-01T00:00:00Z"), null)).toBe(
      "/api/admin/query-history/start-export?start=2026-01-01T00%3A00%3A00.000Z"
    );
  });

  test("download encodes the request ID", () => {
    expect(downloadExportUrl("a b")).toBe(
      "/api/admin/query-history/download?request_id=a%20b"
    );
  });
});

describe("hasPendingExport", () => {
  test("polls only while an export is pending", () => {
    expect(hasPendingExport(undefined)).toBe(false);
    expect(hasPendingExport([exportWith("SUCCESS")])).toBe(false);
    expect(
      hasPendingExport([exportWith("FAILURE"), exportWith("PENDING")])
    ).toBe(true);
  });
});

describe("previewText", () => {
  test("flattens whitespace and cuts long text", () => {
    expect(previewText("a\n\n b  c")).toBe("a b c");
    expect(previewText("abcdef", 4)).toBe("abc…");
  });
});
