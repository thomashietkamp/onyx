/** Shapes of `/api/admin/log-export`. */

export type LogExportState = "collecting" | "ready";

export type LogExportReceiptStatus =
  | "uploaded"
  | "no_logs"
  | "duplicate_host"
  | "failed";

/** What one piece of an export (a worker, the API server, Kubernetes) reported. */
export interface LogExportReceipt {
  worker_name: string;
  hostname: string;
  status: LogExportReceiptStatus;
  file_count: number;
  /** The error of a failed piece, or why an uploaded piece is incomplete. */
  error: string | null;
  file_id: string | null;
  reported_at: string;
}

export interface LogExportStatus {
  export_id: string;
  state: LogExportState;
  started_at: string;
  deadline: string;
  receipts: LogExportReceipt[];
  /** Pieces with no receipt. Once the export is ready, they missed the deadline. */
  pending_worker_names: string[];
}

export interface LogExportStartResponse {
  export_id: string;
}

export type LogExportStartResult =
  | { kind: "started"; exportId: string }
  | { kind: "alreadyRunning" };
