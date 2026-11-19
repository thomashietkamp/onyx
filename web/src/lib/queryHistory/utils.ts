import { buildApiPath } from "@/lib/urlBuilder";
import { SWR_KEYS } from "@/lib/swr-keys";
import type {
  QAFeedbackType,
  QueryHistoryExport,
  QueryHistoryFilters,
} from "@/lib/queryHistory/types";

const FEEDBACK_TYPES: readonly QAFeedbackType[] = ["like", "dislike", "mixed"];

/** Narrows a select value to a feedback type; anything else means "any". */
export function toFeedbackType(value: string): QAFeedbackType | null {
  return FEEDBACK_TYPES.find((type) => type === value) ?? null;
}

/** The SWR key of one page of the query history table. */
export function chatSessionHistoryUrl(filters: QueryHistoryFilters): string {
  return buildApiPath(SWR_KEYS.adminChatSessionHistory, {
    page_num: filters.pageNum,
    page_size: filters.pageSize,
    feedback_type: filters.feedbackType ?? undefined,
    start_time: filters.start?.toISOString(),
    end_time: filters.end?.toISOString(),
  });
}

export function startExportUrl(start: Date | null, end: Date | null): string {
  return buildApiPath("/api/admin/query-history/start-export", {
    start: start?.toISOString(),
    end: end?.toISOString(),
  });
}

export function downloadExportUrl(requestId: string): string {
  return buildApiPath("/api/admin/query-history/download", {
    request_id: requestId,
  });
}

/** Whether the exports list should poll again: some export is not done. */
export function hasPendingExport(
  exports: QueryHistoryExport[] | undefined
): boolean {
  return (exports ?? []).some((item) => item.status === "PENDING");
}

/** A short preview of a message for a table cell, on one line. */
export function previewText(text: string, maxLength = 200): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > maxLength ? `${flat.slice(0, maxLength - 1)}…` : flat;
}
