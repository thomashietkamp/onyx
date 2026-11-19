import { parseErrorDetail } from "@/lib/fetcher";
import type { JsonValue } from "@/lib/json";
import {
  readIsSearchFlow,
  readSearchFullResponse,
  readSearchHistory,
  readSearchStreamPacket,
} from "@/lib/search/searchParsing";
import type {
  BaseFilters,
  SearchDocWithContent,
  SearchFlowClassificationRequest,
  SearchFullResponse,
  SearchHistoryResponse,
  SearchStreamPacket,
  SendSearchQueryRequest,
} from "@/lib/search/types";

const SEARCH_PREFIX = "/api/search";

/** Sections the LLM reads to pick the best documents in search mode. */
export const SEARCH_MODE_LLM_SELECTION_DOCS = 15;
export const SEARCH_MODE_NUM_HITS = 30;

export interface DocumentSearchOptions {
  filters?: BaseFilters;
  numHits?: number;
  signal?: AbortSignal;
}

/** Thrown when a search request fails. `message` is the backend detail. */
export class SearchRequestError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "SearchRequestError";
  }
}

async function throwIfFailed(response: Response): Promise<void> {
  if (response.ok) return;
  const detail = await parseErrorDetail(response, response.statusText);
  throw new SearchRequestError(detail, response.status);
}

async function readJson(response: Response): Promise<JsonValue> {
  await throwIfFailed(response);
  const body: JsonValue = await response.json();
  return body;
}

function postJson(
  path: string,
  body: SendSearchQueryRequest | SearchFlowClassificationRequest,
  signal?: AbortSignal
): Promise<Response> {
  return fetch(`${SEARCH_PREFIX}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

/**
 * Document search with no LLM step, for the browse panes. Results are in
 * retrieval order and filtered by the user's document access.
 */
export async function searchDocuments(
  query: string,
  options?: DocumentSearchOptions
): Promise<SearchFullResponse> {
  const request: SendSearchQueryRequest = {
    search_query: query,
    filters: options?.filters ?? null,
    num_hits: options?.numHits,
    run_query_expansion: false,
    num_docs_fed_to_llm_selection: 0,
    stream: false,
  };
  const response = await postJson(
    "/send-search-message",
    request,
    options?.signal
  );
  const parsed = readSearchFullResponse(await readJson(response));
  if (!parsed) {
    throw new SearchRequestError("Malformed search response", response.status);
  }
  return parsed;
}

/** True when the backend routes the query to search mode, not chat. */
export async function classifySearchFlow(
  query: string,
  signal?: AbortSignal
): Promise<boolean> {
  const response = await postJson(
    "/search-flow-classification",
    { user_query: query },
    signal
  );
  return readIsSearchFlow(await readJson(response));
}

export async function fetchSearchHistory(
  limit?: number,
  signal?: AbortSignal
): Promise<SearchHistoryResponse> {
  const params = new URLSearchParams();
  if (limit !== undefined) params.set("limit", String(limit));
  const response = await fetch(
    `${SEARCH_PREFIX}/search-history?${params.toString()}`,
    { signal }
  );
  return readSearchHistory(await readJson(response));
}

// ---------------------------------------------------------------------------
// Streaming search (search mode)
// ---------------------------------------------------------------------------

export interface SearchStreamChunk {
  packets: SearchStreamPacket[];
  /** The unfinished last line, kept for the next chunk. */
  rest: string;
}

/** Splits buffered NDJSON text into packets and the unfinished last line. */
export function parseSearchStreamChunk(buffer: string): SearchStreamChunk {
  const lines = buffer.split("\n");
  const rest = lines.pop() ?? "";
  const packets: SearchStreamPacket[] = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const parsed: JsonValue = JSON.parse(line);
      const packet = readSearchStreamPacket(parsed);
      if (packet) packets.push(packet);
    } catch {
      // A malformed line carries nothing the page can show.
    }
  }
  return { packets, rest };
}

export interface SearchStreamState {
  executedQueries: string[];
  searchDocs: SearchDocWithContent[];
  llmSelectedDocIds: string[] | null;
  error: string | null;
  /** True once the ranked documents arrived. */
  hasDocs: boolean;
}

export const EMPTY_SEARCH_STREAM_STATE: SearchStreamState = {
  executedQueries: [],
  searchDocs: [],
  llmSelectedDocIds: null,
  error: null,
  hasDocs: false,
};

export function applySearchPacket(
  state: SearchStreamState,
  packet: SearchStreamPacket
): SearchStreamState {
  switch (packet.type) {
    case "search_docs":
      return { ...state, searchDocs: packet.search_docs, hasDocs: true };
    case "llm_selected_docs":
      return { ...state, llmSelectedDocIds: packet.llm_selected_doc_ids };
    case "query_expansions":
      return { ...state, executedQueries: packet.executed_queries };
    case "search_error":
      return { ...state, error: packet.error };
    case "doc_selection_reasoning":
      return state;
  }
}

/**
 * Runs a search mode query and yields each packet as it arrives: the
 * executed queries, then the ranked documents, then the LLM picks.
 */
export async function* streamSearchQuery(
  query: string,
  filters: BaseFilters | null,
  signal?: AbortSignal
): AsyncGenerator<SearchStreamPacket, void, unknown> {
  const request: SendSearchQueryRequest = {
    search_query: query,
    filters,
    num_hits: SEARCH_MODE_NUM_HITS,
    run_query_expansion: true,
    num_docs_fed_to_llm_selection: SEARCH_MODE_LLM_SELECTION_DOCS,
    stream: true,
  };
  const response = await postJson("/send-search-message", request, signal);
  await throwIfFailed(response);

  const reader = response.body?.getReader();
  if (!reader) throw new SearchRequestError("Empty search response", 500);
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSearchStreamChunk(buffer);
      buffer = parsed.rest;
      yield* parsed.packets;
    }
    yield* parseSearchStreamChunk(`${buffer}\n`).packets;
  } finally {
    void reader.cancel().catch(() => {});
  }
}

export interface RankedSearchDoc {
  doc: SearchDocWithContent;
  isLlmPick: boolean;
}

/**
 * The LLM picks first, in the order the LLM gave, then the other documents in
 * retrieval order. One entry per document id.
 */
export function rankSearchResults(
  docs: SearchDocWithContent[],
  llmSelectedDocIds: string[] | null
): RankedSearchDoc[] {
  const byId = new Map<string, SearchDocWithContent>();
  for (const doc of docs) {
    if (!byId.has(doc.document_id)) byId.set(doc.document_id, doc);
  }
  const picks = (llmSelectedDocIds ?? []).filter((id) => byId.has(id));
  const pickSet = new Set(picks);
  const ranked: RankedSearchDoc[] = [];
  for (const id of picks) {
    const doc = byId.get(id);
    if (doc) ranked.push({ doc, isLlmPick: true });
  }
  for (const doc of byId.values()) {
    if (!pickSet.has(doc.document_id)) ranked.push({ doc, isLlmPick: false });
  }
  return ranked;
}

/**
 * Filters with the updated-at range set from a picked date range. `to` covers
 * its whole day. No range clears the time filter and keeps the others.
 */
export function withUpdatedAtRange(
  filters: BaseFilters | null,
  range: { from: Date; to: Date } | undefined
): BaseFilters {
  if (!range) return { ...filters, updated_at_range: null };
  const end = new Date(range.to);
  end.setHours(23, 59, 59, 999);
  return {
    ...filters,
    updated_at_range: {
      start: range.from.toISOString(),
      end: end.toISOString(),
    },
  };
}
