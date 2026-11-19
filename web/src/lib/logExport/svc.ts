import { downloadFile } from "@/lib/download";
import { fetchErrorFrom } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type {
  LogExportStartResponse,
  LogExportStartResult,
} from "@/lib/logExport/types";

const ALREADY_RUNNING_STATUS = 429;
const DEFAULT_BUNDLE_NAME = "onyx_logs.zip";

/** Starts an export. A second start while one collects gets a 429. */
export async function startLogExport(): Promise<LogExportStartResult> {
  const response = await fetch(SWR_KEYS.logExport, { method: "POST" });
  if (response.status === ALREADY_RUNNING_STATUS) {
    return { kind: "alreadyRunning" };
  }
  if (!response.ok) {
    throw await fetchErrorFrom(response, response.statusText);
  }
  const body: LogExportStartResponse = await response.json();
  return { kind: "started", exportId: body.export_id };
}

function bundleNameFrom(contentDisposition: string | null): string {
  const match = contentDisposition?.match(/filename="?([^";]+)"?/);
  return match?.[1] ?? DEFAULT_BUNDLE_NAME;
}

/** Fetches the bundle of a ready export and saves it. */
export async function downloadLogExport(exportId: string): Promise<void> {
  const response = await fetch(SWR_KEYS.logExportDownload(exportId));
  if (!response.ok) {
    throw await fetchErrorFrom(response, response.statusText);
  }
  const blob = await response.blob();
  downloadFile(bundleNameFrom(response.headers.get("Content-Disposition")), {
    content: blob,
  });
}
