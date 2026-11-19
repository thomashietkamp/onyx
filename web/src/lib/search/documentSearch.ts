import type { BaseFilters, SearchFullResponse } from "@/lib/search/types";

export interface DocumentSearchOptions {
  filters?: BaseFilters;
  numHits?: number;
  signal?: AbortSignal;
}

/**
 * Document search for the browse panes. This build has no search-message
 * route yet, so the call always fails and callers show their error state.
 */
export async function searchDocuments(
  _query: string,
  _options?: DocumentSearchOptions
): Promise<SearchFullResponse> {
  throw new Error("Document search is not available in this build.");
}
