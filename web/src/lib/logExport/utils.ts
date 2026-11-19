import type { LogExportReceipt, LogExportStatus } from "@/lib/logExport/types";

/** A catalog key under `admin.exportLogs` and its ICU arguments. */
export interface LogExportPieceLabel {
  key:
    | "receipt.uploaded.label"
    | "receipt.uploadedWithWarning.label"
    | "receipt.noLogs.label"
    | "receipt.duplicateHost.label"
    | "receipt.failed.label"
    | "receipt.failedWithError.label"
    | "worker.collecting.label"
    | "worker.missedDeadline.label";
  values?: { count?: number; error?: string };
}

export interface LogExportPiece {
  workerName: string;
  label: LogExportPieceLabel;
}

export function receiptLabel(receipt: LogExportReceipt): LogExportPieceLabel {
  switch (receipt.status) {
    case "uploaded":
      return receipt.error
        ? {
            key: "receipt.uploadedWithWarning.label",
            values: { count: receipt.file_count, error: receipt.error },
          }
        : {
            key: "receipt.uploaded.label",
            values: { count: receipt.file_count },
          };
    case "no_logs":
      return { key: "receipt.noLogs.label" };
    case "duplicate_host":
      return { key: "receipt.duplicateHost.label" };
    case "failed":
      return receipt.error
        ? {
            key: "receipt.failedWithError.label",
            values: { error: receipt.error },
          }
        : { key: "receipt.failed.label" };
  }
}

/** One row for each piece: reported pieces first, then the ones still out. */
export function logExportPieces(status: LogExportStatus): LogExportPiece[] {
  const pendingLabel: LogExportPieceLabel =
    status.state === "ready"
      ? { key: "worker.missedDeadline.label" }
      : { key: "worker.collecting.label" };
  return [
    ...status.receipts.map((receipt) => ({
      workerName: receipt.worker_name,
      label: receiptLabel(receipt),
    })),
    ...status.pending_worker_names.map((workerName) => ({
      workerName,
      label: pendingLabel,
    })),
  ];
}
