import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type {
  ChatSessionHistoryPage,
  ChatSessionSnapshot,
  QueryHistoryExport,
  QueryHistoryFilters,
} from "@/lib/queryHistory/types";
import {
  chatSessionHistoryUrl,
  hasPendingExport,
} from "@/lib/queryHistory/utils";

const EXPORT_POLL_MS = 3000;

export function useChatSessionHistory(filters: QueryHistoryFilters) {
  return useSWR<ChatSessionHistoryPage>(
    chatSessionHistoryUrl(filters),
    errorHandlingFetcher,
    { keepPreviousData: true }
  );
}

export function useChatSessionSnapshot(id: string | undefined) {
  return useSWR<ChatSessionSnapshot>(
    id ? SWR_KEYS.adminChatSession(id) : null,
    errorHandlingFetcher
  );
}

/** The previous exports. Polls while one is still pending. */
export function useQueryHistoryExports(enabled: boolean) {
  return useSWR<QueryHistoryExport[]>(
    enabled ? SWR_KEYS.adminQueryHistoryExports : null,
    errorHandlingFetcher,
    {
      refreshInterval: (data) => (hasPendingExport(data) ? EXPORT_POLL_MS : 0),
    }
  );
}
