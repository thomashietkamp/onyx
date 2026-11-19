// Readers that turn decoded search-mode JSON into typed values, without casts.
import { ValidSources } from "@/lib/connectors/types/source";
import {
  isJsonArray,
  isJsonObject,
  readArray,
  readNumber,
  readObject,
  readString,
  type JsonObject,
  type JsonValue,
} from "@/lib/json";
import type {
  SearchDocWithContent,
  SearchFullResponse,
  SearchHistoryResponse,
  SearchQueryResponse,
  SearchStreamPacket,
} from "@/lib/search/types";

const VALID_SOURCES: readonly ValidSources[] = Object.values(ValidSources);

function readSource(value: JsonValue | undefined): ValidSources | null {
  return VALID_SOURCES.find((source) => source === value) ?? null;
}

function readStringList(value: JsonValue | undefined): string[] | null {
  if (!isJsonArray(value)) return null;
  const strings: string[] = [];
  for (const entry of value) {
    if (typeof entry !== "string") return null;
    strings.push(entry);
  }
  return strings;
}

function readMetadata(
  source: JsonObject
): Record<string, string | string[]> | null {
  const metadata = readObject(source, "metadata");
  if (!metadata) return {};
  const result: Record<string, string | string[]> = {};
  for (const [key, value] of Object.entries(metadata)) {
    if (typeof value === "string") {
      result[key] = value;
      continue;
    }
    const list = readStringList(value);
    if (list === null) return null;
    result[key] = list;
  }
  return result;
}

export function readSearchDoc(
  value: JsonValue | undefined
): SearchDocWithContent | null {
  if (!isJsonObject(value)) return null;
  const documentId = readString(value, "document_id");
  const sourceType = readSource(value.source_type);
  const metadata = readMetadata(value);
  if (documentId === null || sourceType === null || metadata === null) {
    return null;
  }
  const sourceTypes = isJsonArray(value.source_types)
    ? value.source_types
        .map(readSource)
        .filter((source): source is ValidSources => source !== null)
    : null;
  return {
    document_id: documentId,
    chunk_ind: readNumber(value, "chunk_ind") ?? 0,
    semantic_identifier: readString(value, "semantic_identifier") ?? documentId,
    link: readString(value, "link"),
    blurb: readString(value, "blurb") ?? "",
    source_type: sourceType,
    source_types: sourceTypes,
    boost: readNumber(value, "boost") ?? 0,
    hidden: value.hidden === true,
    metadata,
    score: readNumber(value, "score"),
    is_relevant:
      typeof value.is_relevant === "boolean" ? value.is_relevant : null,
    relevance_explanation: readString(value, "relevance_explanation"),
    match_highlights: readStringList(value.match_highlights) ?? [],
    updated_at: readString(value, "updated_at"),
    primary_owners: readStringList(value.primary_owners),
    secondary_owners: readStringList(value.secondary_owners),
    is_internet: value.is_internet === true,
    content: readString(value, "content"),
  };
}

/** Every entry must be a document; a list with a bad entry reads as null. */
export function readSearchDocs(
  value: JsonValue[] | null
): SearchDocWithContent[] | null {
  if (value === null) return null;
  const docs: SearchDocWithContent[] = [];
  for (const entry of value) {
    const doc = readSearchDoc(entry);
    if (doc === null) return null;
    docs.push(doc);
  }
  return docs;
}

/** One NDJSON line of the search stream; null when it is not a known packet. */
export function readSearchStreamPacket(
  value: JsonValue
): SearchStreamPacket | null {
  if (!isJsonObject(value)) return null;
  switch (readString(value, "type")) {
    case "search_docs": {
      const docs = readSearchDocs(readArray(value, "search_docs"));
      return docs ? { type: "search_docs", search_docs: docs } : null;
    }
    case "search_error": {
      const error = readString(value, "error");
      return error !== null ? { type: "search_error", error } : null;
    }
    case "llm_selected_docs": {
      if (value.llm_selected_doc_ids === null) {
        return { type: "llm_selected_docs", llm_selected_doc_ids: null };
      }
      const ids = readStringList(value.llm_selected_doc_ids);
      return ids
        ? { type: "llm_selected_docs", llm_selected_doc_ids: ids }
        : null;
    }
    case "query_expansions": {
      const queries = readStringList(value.executed_queries);
      return queries
        ? { type: "query_expansions", executed_queries: queries }
        : null;
    }
    case "doc_selection_reasoning": {
      const reasoning = readString(value, "reasoning");
      return reasoning !== null
        ? { type: "doc_selection_reasoning", reasoning }
        : null;
    }
    default:
      return null;
  }
}

export function readSearchFullResponse(
  value: JsonValue
): SearchFullResponse | null {
  if (!isJsonObject(value)) return null;
  const docs = readSearchDocs(readArray(value, "search_docs"));
  if (docs === null) return null;
  return {
    all_executed_queries: readStringList(value.all_executed_queries) ?? [],
    search_docs: docs,
    doc_selection_reasoning: readString(value, "doc_selection_reasoning"),
    llm_selected_doc_ids: readStringList(value.llm_selected_doc_ids),
    error: readString(value, "error"),
  };
}

export function readSearchHistory(value: JsonValue): SearchHistoryResponse {
  const entries = isJsonObject(value)
    ? (readArray(value, "search_queries") ?? [])
    : [];
  const searchQueries: SearchQueryResponse[] = [];
  for (const entry of entries) {
    if (!isJsonObject(entry)) continue;
    const query = readString(entry, "query");
    const createdAt = readString(entry, "created_at");
    if (query === null || createdAt === null) continue;
    searchQueries.push({
      query,
      query_expansions: readStringList(entry.query_expansions),
      created_at: createdAt,
    });
  }
  return { search_queries: searchQueries };
}

export function readIsSearchFlow(value: JsonValue): boolean {
  return isJsonObject(value) && value.is_search_flow === true;
}
