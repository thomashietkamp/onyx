import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { LogExportStatus } from "@/lib/logExport/types";

const POLL_INTERVAL_MS = 2_000;

/** Polls the status of an export until it is ready. `null` reads nothing. */
export function useLogExportStatus(exportId: string | null) {
  const { data, error } = useSWR<LogExportStatus>(
    exportId ? SWR_KEYS.logExportStatus(exportId) : null,
    errorHandlingFetcher,
    {
      refreshInterval: (latest) =>
        latest?.state === "ready" ? 0 : POLL_INTERVAL_MS,
      // A failed read ends the watch; the page reports it.
      shouldRetryOnError: false,
      revalidateOnFocus: false,
    }
  );
  return { status: data, error };
}
