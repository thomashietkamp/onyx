import { ValidSources } from "@/lib/connectors/types/source";
import type { SearchDocWithContent } from "@/lib/search/types";
import {
  applySearchPacket,
  EMPTY_SEARCH_STREAM_STATE,
  parseSearchStreamChunk,
  rankSearchResults,
  withUpdatedAtRange,
} from "@/lib/search/documentSearch";
import { readSearchStreamPacket } from "@/lib/search/searchParsing";

function makeDoc(documentId: string, chunkInd = 0): SearchDocWithContent {
  return {
    document_id: documentId,
    chunk_ind: chunkInd,
    semantic_identifier: documentId,
    link: null,
    blurb: "",
    source_type: ValidSources.Confluence,
    boost: 0,
    hidden: false,
    metadata: {},
    score: 1,
    match_highlights: [],
    updated_at: null,
    is_internet: false,
  };
}

describe("parseSearchStreamChunk", () => {
  it("returns complete packets and keeps the unfinished line", () => {
    const docs = JSON.stringify({
      type: "search_docs",
      search_docs: [makeDoc("a")],
    });
    const { packets, rest } = parseSearchStreamChunk(
      `${docs}\n{"type":"llm_selected_docs","llm_sel`
    );
    expect(packets).toHaveLength(1);
    const [packet] = packets;
    expect(packet?.type).toBe("search_docs");
    if (packet?.type === "search_docs") {
      expect(packet.search_docs).toEqual([
        expect.objectContaining({
          document_id: "a",
          source_type: ValidSources.Confluence,
          content: null,
        }),
      ]);
    }
    expect(rest).toBe('{"type":"llm_selected_docs","llm_sel');
  });

  it("drops malformed and unknown lines", () => {
    const { packets } = parseSearchStreamChunk(
      'not json\n{"type":"other"}\n{"type":"search_error","error":"boom"}\n'
    );
    expect(packets).toEqual([{ type: "search_error", error: "boom" }]);
  });
});

describe("readSearchStreamPacket", () => {
  it("rejects packets with the wrong field types", () => {
    expect(
      readSearchStreamPacket({
        type: "query_expansions",
        executed_queries: [1],
      })
    ).toBeNull();
    expect(
      readSearchStreamPacket({ type: "search_docs", search_docs: [{}] })
    ).toBeNull();
    expect(
      readSearchStreamPacket({
        type: "search_docs",
        search_docs: [{ document_id: "a", source_type: "not_a_source" }],
      })
    ).toBeNull();
    expect(
      readSearchStreamPacket({
        type: "llm_selected_docs",
        llm_selected_doc_ids: null,
      })
    ).toEqual({ type: "llm_selected_docs", llm_selected_doc_ids: null });
  });
});

describe("applySearchPacket", () => {
  it("folds the packets of one search", () => {
    let state = EMPTY_SEARCH_STREAM_STATE;
    state = applySearchPacket(state, {
      type: "query_expansions",
      executed_queries: ["q", "q rephrased"],
    });
    expect(state.hasDocs).toBe(false);
    state = applySearchPacket(state, {
      type: "search_docs",
      search_docs: [makeDoc("a")],
    });
    state = applySearchPacket(state, {
      type: "llm_selected_docs",
      llm_selected_doc_ids: ["a"],
    });
    expect(state).toEqual({
      executedQueries: ["q", "q rephrased"],
      searchDocs: [makeDoc("a")],
      llmSelectedDocIds: ["a"],
      error: null,
      hasDocs: true,
    });
  });
});

describe("rankSearchResults", () => {
  it("puts the LLM picks first and keeps one entry per document", () => {
    const docs = [makeDoc("a"), makeDoc("b"), makeDoc("a", 3), makeDoc("c")];
    const ranked = rankSearchResults(docs, ["c", "missing", "a"]);
    expect(
      ranked.map(({ doc, isLlmPick }) => [doc.document_id, isLlmPick])
    ).toEqual([
      ["c", true],
      ["a", true],
      ["b", false],
    ]);
  });

  it("keeps retrieval order without picks", () => {
    const ranked = rankSearchResults([makeDoc("b"), makeDoc("a")], null);
    expect(ranked.map(({ doc }) => doc.document_id)).toEqual(["b", "a"]);
  });
});

describe("withUpdatedAtRange", () => {
  it("sets the range to the end of the last day and keeps other filters", () => {
    const from = new Date(2026, 0, 1, 10);
    const to = new Date(2026, 0, 31, 8);
    const filters = withUpdatedAtRange(
      { source_type: [ValidSources.Confluence] },
      { from, to }
    );
    expect(filters.source_type).toEqual([ValidSources.Confluence]);
    expect(filters.updated_at_range?.start).toBe(from.toISOString());
    expect(filters.updated_at_range?.end).toBe(
      new Date(2026, 0, 31, 23, 59, 59, 999).toISOString()
    );
  });

  it("clears the range", () => {
    const filters = withUpdatedAtRange(
      { updated_at_range: { start: "x", end: "y" } },
      undefined
    );
    expect(filters.updated_at_range).toBeNull();
  });
});
