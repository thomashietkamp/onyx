import { logExportPieces, receiptLabel } from "@/lib/logExport/utils";
import type { LogExportReceipt, LogExportStatus } from "@/lib/logExport/types";

function receipt(overrides: Partial<LogExportReceipt>): LogExportReceipt {
  return {
    worker_name: "primary",
    hostname: "host",
    status: "uploaded",
    file_count: 3,
    error: null,
    file_id: "file",
    reported_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function status(overrides: Partial<LogExportStatus>): LogExportStatus {
  return {
    export_id: "id",
    state: "collecting",
    started_at: "2026-01-01T00:00:00Z",
    deadline: "2026-01-01T00:01:30Z",
    receipts: [],
    pending_worker_names: [],
    ...overrides,
  };
}

describe("receiptLabel", () => {
  it("counts the files of an uploaded piece", () => {
    expect(receiptLabel(receipt({}))).toEqual({
      key: "receipt.uploaded.label",
      values: { count: 3 },
    });
  });

  it("names the reason an uploaded piece is incomplete", () => {
    expect(receiptLabel(receipt({ error: "size limit" }))).toEqual({
      key: "receipt.uploadedWithWarning.label",
      values: { count: 3, error: "size limit" },
    });
  });

  it("shows the error of a failed piece only when there is one", () => {
    expect(receiptLabel(receipt({ status: "failed", error: "boom" }))).toEqual({
      key: "receipt.failedWithError.label",
      values: { error: "boom" },
    });
    expect(receiptLabel(receipt({ status: "failed" }))).toEqual({
      key: "receipt.failed.label",
    });
  });

  it("labels empty and duplicate pieces", () => {
    expect(receiptLabel(receipt({ status: "no_logs" })).key).toBe(
      "receipt.noLogs.label"
    );
    expect(receiptLabel(receipt({ status: "duplicate_host" })).key).toBe(
      "receipt.duplicateHost.label"
    );
  });
});

describe("logExportPieces", () => {
  it("lists reported pieces, then the pieces still collecting", () => {
    const pieces = logExportPieces(
      status({
        receipts: [receipt({ worker_name: "api_server" })],
        pending_worker_names: ["heavy"],
      })
    );
    expect(pieces.map((piece) => piece.workerName)).toEqual([
      "api_server",
      "heavy",
    ]);
    expect(pieces[1]?.label.key).toBe("worker.collecting.label");
  });

  it("marks pending pieces of a ready export as late", () => {
    const pieces = logExportPieces(
      status({ state: "ready", pending_worker_names: ["heavy"] })
    );
    expect(pieces[0]?.label.key).toBe("worker.missedDeadline.label");
  });
});
