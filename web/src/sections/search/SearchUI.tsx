"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { useTranslations } from "next-intl";
import {
  InputDateRangePicker,
  SelectButton,
  Text,
  type DateRange,
} from "@opal/components";
import { IllustrationContent, Section } from "@opal/layouts";
import { LineLoader } from "@opal/loaders";
import { SvgNoResult, SvgTimeout } from "@opal/illustrations";
import { getSourceMetadata } from "@/lib/connectors/utils";
import type { ValidSources } from "@/lib/connectors/types/source";
import {
  fetchSearchHistory,
  rankSearchResults,
  withUpdatedAtRange,
} from "@/lib/search/documentSearch";
import type {
  MinimalOnyxDocument,
  SearchDocWithContent,
} from "@/lib/search/types";
import {
  countDocumentsBySource,
  documentMatchesAnySource,
  openExternalLink,
  recentQueries,
} from "@/lib/search/utils";
import { useQueryController } from "@/providers/QueryControllerProvider";
import SearchResultCard from "@/sections/search/SearchResultCard";

const RECENT_SEARCH_LIMIT = 6;
const SEARCH_HISTORY_KEY = "search-history";

export interface SearchUIProps {
  onDocumentClick: (doc: MinimalOnyxDocument) => void;
}

/**
 * Search mode results: the ranked documents of the last search, with the
 * LLM's picks first. Source chips filter the shown results; the date range
 * runs the search again with a server-side time filter.
 */
export default function SearchUI({ onDocumentClick }: SearchUIProps) {
  const t = useTranslations("searchMode");
  const {
    state,
    searchResults,
    llmSelectedDocIds,
    executedQueries,
    currentQuery,
    currentFilters,
    error,
    refineSearch,
    runQuery,
  } = useQueryController();
  const history = useSWR(
    // Refetched after each search, so the list includes it.
    [SEARCH_HISTORY_KEY, currentQuery],
    () => fetchSearchHistory(RECENT_SEARCH_LIMIT * 3),
    { revalidateOnFocus: false }
  );
  const recent = recentQueries(
    (history.data?.search_queries ?? []).map((entry) => entry.query),
    currentQuery,
    RECENT_SEARCH_LIMIT
  );
  const [sourceFilter, setSourceFilter] = useState<ValidSources | null>(null);
  const [dateRange, setDateRange] = useState<DateRange>(undefined);

  const ranked = useMemo(
    () => rankSearchResults(searchResults, llmSelectedDocIds),
    [searchResults, llmSelectedDocIds]
  );
  const sourceCounts = useMemo(
    () => countDocumentsBySource(searchResults),
    [searchResults]
  );
  const shown = useMemo(
    () =>
      sourceFilter
        ? ranked.filter(({ doc }) =>
            documentMatchesAnySource(doc, [sourceFilter])
          )
        : ranked,
    [ranked, sourceFilter]
  );
  const expansions = executedQueries.filter((q) => q !== currentQuery);

  function handleOpen(doc: SearchDocWithContent) {
    if (doc.link) {
      openExternalLink(doc.link);
      return;
    }
    onDocumentClick(doc);
  }

  function handleDateRangeChange(range: DateRange) {
    setDateRange(range);
    void refineSearch(withUpdatedAtRange(currentFilters, range));
  }

  const isLoading = state.phase === "searching" && searchResults.length === 0;

  let body: React.ReactNode;
  if (isLoading) {
    body = (
      <Section gap={0.5} alignItems="stretch" height="auto">
        <Text font="secondary-body" color="text-03" as="p">
          {t("loading.description")}
        </Text>
        <LineLoader lines={6} />
      </Section>
    );
  } else if (error && searchResults.length === 0) {
    body = (
      <IllustrationContent
        illustration={SvgTimeout}
        title={t("error.title")}
        description={error}
      />
    );
  } else if (shown.length === 0) {
    body = (
      <IllustrationContent
        illustration={SvgNoResult}
        title={t("empty.title")}
        description={t("empty.description")}
      />
    );
  } else {
    body = (
      <Section gap={0.25} alignItems="stretch" height="auto">
        {shown.map(({ doc, isLlmPick }) => (
          <SearchResultCard
            key={`${doc.document_id}-${doc.chunk_ind}`}
            doc={doc}
            isLlmPick={isLlmPick}
            onOpen={handleOpen}
          />
        ))}
      </Section>
    );
  }

  return (
    <Section
      gap={0.75}
      alignItems="stretch"
      justifyContent="start"
      className="min-h-0 overflow-y-auto"
      data-testid="search-ui"
    >
      <Section
        flexDirection="row"
        alignItems="center"
        justifyContent="between"
        gap={0.5}
        height="auto"
        className="flex-wrap"
      >
        <Text font="secondary-body" color="text-03" as="p">
          {isLoading
            ? t("loading.description")
            : t("results.count", { count: shown.length })}
        </Text>
        <InputDateRangePicker
          size="sm"
          value={dateRange}
          onValueChange={handleDateRangeChange}
        />
      </Section>

      {sourceCounts.size > 1 && (
        <Section
          flexDirection="row"
          alignItems="center"
          justifyContent="start"
          gap={0.25}
          height="auto"
          className="flex-wrap"
        >
          <SelectButton
            size="sm"
            variant="select-filter"
            state={sourceFilter === null ? "selected" : "empty"}
            onClick={() => setSourceFilter(null)}
          >
            {t("filters.allSources", { count: searchResults.length })}
          </SelectButton>
          {[...sourceCounts.entries()].map(([source, count]) => {
            const meta = getSourceMetadata(source);
            return (
              <SelectButton
                key={source}
                size="sm"
                variant="select-filter"
                icon={meta.icon}
                state={sourceFilter === source ? "selected" : "empty"}
                onClick={() =>
                  setSourceFilter(sourceFilter === source ? null : source)
                }
              >
                {t("filters.source", { source: meta.displayName, count })}
              </SelectButton>
            );
          })}
        </Section>
      )}

      {expansions.length > 0 && (
        <Text font="secondary-body" color="text-04" as="p" maxLines={1}>
          {t("results.alsoSearched", { queries: expansions.join(", ") })}
        </Text>
      )}

      {body}

      {recent.length > 0 && state.phase !== "searching" && (
        <Section gap={0.25} alignItems="start" height="auto">
          <Text font="secondary-body" color="text-03" as="p">
            {t("history.title")}
          </Text>
          <Section
            flexDirection="row"
            alignItems="center"
            justifyContent="start"
            gap={0.25}
            height="auto"
            className="flex-wrap"
          >
            {recent.map((query) => (
              <SelectButton
                key={query}
                size="sm"
                variant="select-filter"
                state="empty"
                onClick={() => void runQuery(query)}
              >
                {query}
              </SelectButton>
            ))}
          </Section>
        </Section>
      )}
    </Section>
  );
}
