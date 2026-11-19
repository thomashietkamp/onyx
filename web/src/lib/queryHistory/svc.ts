import { parseErrorDetail } from "@/lib/fetcher";
import { downloadExportUrl, startExportUrl } from "@/lib/queryHistory/utils";

export async function startQueryHistoryExport(
  start: Date | null,
  end: Date | null
): Promise<string> {
  const res = await fetch(startExportUrl(start, end), { method: "POST" });
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, res.statusText));
  }
  const body: { request_id: string } = await res.json();
  return body.request_id;
}

export async function downloadQueryHistoryExport(
  requestId: string
): Promise<void> {
  const res = await fetch(downloadExportUrl(requestId));
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, res.statusText));
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `query-history-${requestId}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
