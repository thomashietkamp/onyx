"use client";

import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";
import { SearchDocWithContent, BaseFilters } from "@/lib/search/types";
import { useIsSearchModeAvailable } from "@/lib/settings/hooks";
import { useAppPosition } from "@/lib/navigation/hooks";
import {
  applySearchPacket,
  classifySearchFlow,
  EMPTY_SEARCH_STREAM_STATE,
  SearchRequestError,
  type SearchStreamState,
  streamSearchQuery,
} from "@/lib/search/documentSearch";

export type AppMode = "auto" | "search" | "chat";

export type QueryState =
  | { phase: "idle"; appMode: AppMode }
  | { phase: "classifying" }
  | { phase: "searching" }
  | { phase: "search-results" }
  | { phase: "chat" };

type QueryPhase = QueryState["phase"];

export interface SubmitQueryOptions {
  filters?: BaseFilters;
  /** Routes to chat, for a message that search cannot run, such as one with
   * mentions or in incognito. */
  chatOnly?: boolean;
}

export interface QueryControllerValue {
  /** Single state variable encoding both the query lifecycle phase and (when idle) the user's mode selection. */
  state: QueryState;
  /** Update the app mode. Shown when idle. Search modes fall back to chat while search is unavailable. */
  setAppMode: (mode: AppMode) => void;
  /** Search results (empty if chat or not yet searched) */
  searchResults: SearchDocWithContent[];
  /** Document IDs selected by the LLM as most relevant */
  llmSelectedDocIds: string[] | null;
  /** Queries the last search ran, the typed one and its expansions */
  executedQueries: string[];
  /** The query and filters of the last search, null when none ran */
  currentQuery: string | null;
  currentFilters: BaseFilters | null;
  /** User-facing error message from the last search or classification request, null when idle */
  error: string | null;
  /** Submit a query - routes to search or chat based on app mode */
  submit: (
    query: string,
    onChat: (query: string) => void,
    options?: SubmitQueryOptions
  ) => Promise<void>;
  /** Re-run the current search query with updated server-side filters */
  refineSearch: (filters: BaseFilters) => Promise<void>;
  /** Reset all state to initial values */
  reset: () => void;
}

export const QueryControllerContext = createContext<QueryControllerValue>({
  state: { phase: "idle", appMode: "chat" },
  setAppMode: () => undefined,
  searchResults: [],
  llmSelectedDocIds: null,
  executedQueries: [],
  currentQuery: null,
  currentFilters: null,
  error: null,
  submit: async (_q, onChat) => {
    onChat(_q);
  },
  refineSearch: async () => undefined,
  reset: () => undefined,
});

export function useQueryController(): QueryControllerValue {
  return useContext(QueryControllerContext);
}

/**
 * Owns the new-session query lifecycle. In "search" mode a query runs a
 * document search; in "auto" mode the backend classifies it first; in "chat"
 * mode, or while search mode is unavailable, it goes to chat.
 */
export function QueryControllerProvider({
  children,
}: {
  children?: ReactNode;
}) {
  const t = useTranslations("searchMode");
  const isSearchModeAvailable = useIsSearchModeAvailable();
  const [requestedMode, setRequestedMode] = useState<AppMode>("chat");
  const [phase, setPhase] = useState<QueryPhase>("idle");
  const [results, setResults] = useState<SearchStreamState>(
    EMPTY_SEARCH_STREAM_STATE
  );
  const [currentQuery, setCurrentQuery] = useState<string | null>(null);
  const [currentFilters, setCurrentFilters] = useState<BaseFilters | null>(
    null
  );
  const abortRef = useRef<AbortController | null>(null);

  const appMode: AppMode = isSearchModeAvailable ? requestedMode : "chat";

  const abortInFlight = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  useEffect(() => abortInFlight, [abortInFlight]);

  // Search mode lives on the new-session page (and an agent's start page).
  // Leaving it ends the search, so a chat page never shows search state.
  const appPosition = useAppPosition();
  const onSearchPage = appPosition.isNewSession() || appPosition.isAgent();
  useEffect(() => {
    if (onSearchPage) return;
    abortInFlight();
    setPhase("idle");
    setResults(EMPTY_SEARCH_STREAM_STATE);
    setCurrentQuery(null);
    setCurrentFilters(null);
  }, [onSearchPage, abortInFlight]);

  const runSearch = useCallback(
    async (query: string, filters: BaseFilters | null) => {
      abortInFlight();
      const controller = new AbortController();
      abortRef.current = controller;
      setCurrentQuery(query);
      setCurrentFilters(filters);
      setResults(EMPTY_SEARCH_STREAM_STATE);
      setPhase("searching");

      let state = EMPTY_SEARCH_STREAM_STATE;
      try {
        for await (const packet of streamSearchQuery(
          query,
          filters,
          controller.signal
        )) {
          if (controller.signal.aborted) return;
          state = applySearchPacket(state, packet);
          setResults(state);
          if (state.hasDocs || state.error) setPhase("search-results");
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        const message =
          err instanceof SearchRequestError ? err.message : t("error.generic");
        setResults({ ...state, error: message });
      }
      if (controller.signal.aborted) return;
      if (abortRef.current === controller) abortRef.current = null;
      setPhase("search-results");
    },
    [abortInFlight, t]
  );

  const submit = useCallback(
    async (
      query: string,
      onChat: (query: string) => void,
      options?: SubmitQueryOptions
    ) => {
      const trimmed = query.trim();
      // Chat owns the turn from here; the controller stays idle, so the
      // new-session page keeps its welcome layout until the chat opens.
      const goToChat = () => {
        abortInFlight();
        setPhase("idle");
        onChat(query);
      };

      if (options?.chatOnly || !trimmed || appMode === "chat") {
        goToChat();
        return;
      }

      if (appMode === "auto") {
        abortInFlight();
        const controller = new AbortController();
        abortRef.current = controller;
        setPhase("classifying");
        let isSearchFlow = false;
        try {
          isSearchFlow = await classifySearchFlow(trimmed, controller.signal);
        } catch {
          // Routing must not block the user: an unclassified query is a chat.
          if (controller.signal.aborted) return;
        }
        if (controller.signal.aborted) return;
        if (!isSearchFlow) {
          goToChat();
          return;
        }
      }

      await runSearch(trimmed, options?.filters ?? null);
    },
    [abortInFlight, appMode, runSearch]
  );

  const refineSearch = useCallback(
    async (filters: BaseFilters) => {
      if (currentQuery === null) return;
      await runSearch(currentQuery, filters);
    },
    [currentQuery, runSearch]
  );

  const reset = useCallback(() => {
    abortInFlight();
    setPhase("idle");
    setResults(EMPTY_SEARCH_STREAM_STATE);
    setCurrentQuery(null);
    setCurrentFilters(null);
  }, [abortInFlight]);

  const setAppMode = useCallback((mode: AppMode) => {
    setRequestedMode(mode);
  }, []);

  const state: QueryState = useMemo(
    () => (phase === "idle" ? { phase, appMode } : { phase }),
    [phase, appMode]
  );

  const value = useMemo<QueryControllerValue>(
    () => ({
      state,
      setAppMode,
      searchResults: results.searchDocs,
      llmSelectedDocIds: results.llmSelectedDocIds,
      executedQueries: results.executedQueries,
      currentQuery,
      currentFilters,
      error: results.error,
      submit,
      refineSearch,
      reset,
    }),
    [
      state,
      setAppMode,
      results,
      currentQuery,
      currentFilters,
      submit,
      refineSearch,
      reset,
    ]
  );

  return (
    <QueryControllerContext.Provider value={value}>
      {children}
    </QueryControllerContext.Provider>
  );
}
