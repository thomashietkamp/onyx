import { FetchError } from "@/lib/fetcher";
import type { HookExecutionLog } from "@/lib/hookExtensions/types";
import {
  formErrorKeyForError,
  formErrorKeyForStatus,
  formatHookLogLine,
  groupHookLogs,
  parseCheckStatus,
} from "@/lib/hookExtensions/utils";

function log(createdAt: string, message: string | null): HookExecutionLog {
  return {
    error_message: message,
    status_code: 500,
    duration_ms: 12,
    created_at: createdAt,
  };
}

describe("parseCheckStatus", () => {
  it("reads the status from a failed check detail", () => {
    expect(
      parseCheckStatus("The endpoint check failed (timeout): took too long")
    ).toBe("timeout");
    expect(
      parseCheckStatus("The endpoint check failed (auth_failed): 401")
    ).toBe("auth_failed");
    expect(
      parseCheckStatus("The endpoint check failed (cannot_connect): refused")
    ).toBe("cannot_connect");
  });

  it("returns null for other details", () => {
    expect(parseCheckStatus("Timeout must be positive")).toBeNull();
  });
});

describe("formErrorKeyForError", () => {
  it("maps a 400 check failure to its form error", () => {
    const error = new FetchError("Bad Request", 400, {
      detail: "The endpoint check failed (auth_failed): 401",
    });
    expect(formErrorKeyForError(error)).toBe("invalidApiKey");
  });

  it("falls back to generic for other statuses and errors", () => {
    const conflict = new FetchError("Conflict", 409, {
      detail: "The endpoint check failed (timeout): x",
    });
    expect(formErrorKeyForError(conflict)).toBe("generic");
    expect(formErrorKeyForError(new Error("network"))).toBe("generic");
    expect(formErrorKeyForStatus("passed")).toBe("generic");
  });
});

describe("groupHookLogs", () => {
  it("splits logs at one hour before now and keeps their order", () => {
    const now = Date.parse("2026-01-01T12:00:00Z");
    const logs = [
      log("2026-01-01T11:59:00Z", "a"),
      log("2026-01-01T11:00:00Z", "b"),
      log("2026-01-01T10:59:59Z", "c"),
    ];
    const grouped = groupHookLogs(logs, now);
    expect(grouped.pastHour.map((l) => l.error_message)).toEqual(["a", "b"]);
    expect(grouped.older.map((l) => l.error_message)).toEqual(["c"]);
  });
});

describe("formatHookLogLine", () => {
  it("uses the fallback for a missing message and skips null fields", () => {
    expect(
      formatHookLogLine(
        {
          error_message: null,
          status_code: null,
          duration_ms: 5,
          created_at: "t",
        },
        "Unknown error"
      )
    ).toBe("t 5ms Unknown error");
  });
});
