"use client";

import { useFormatter, useTranslations } from "next-intl";
import { LineItemButton, Text } from "@opal/components";
import { getSourceMetadata } from "@/lib/connectors/utils";
import type { SearchDocWithContent } from "@/lib/search/types";

export interface SearchResultCardProps {
  doc: SearchDocWithContent;
  isLlmPick: boolean;
  onOpen: (doc: SearchDocWithContent) => void;
}

/** Strips the `<hi>` match markers the index puts around highlighted words. */
function plainText(text: string): string {
  return text.replace(/<\/?hi>/g, "");
}

export default function SearchResultCard({
  doc,
  isLlmPick,
  onOpen,
}: SearchResultCardProps) {
  const t = useTranslations("searchMode");
  const format = useFormatter();
  const sourceMeta = getSourceMetadata(doc.source_type);
  const highlight = doc.match_highlights.find((h) => h.trim() !== "");
  const description = plainText(highlight ?? doc.blurb).trim();

  return (
    <LineItemButton
      icon={sourceMeta.icon}
      title={doc.semantic_identifier || doc.document_id}
      titleMaxLines={1}
      description={description || undefined}
      descriptionMaxLines={2}
      badge={
        isLlmPick ? { title: t("results.bestMatch"), color: "info" } : undefined
      }
      rightChildren={
        doc.updated_at ? (
          <Text font="secondary-body" color="text-03" maxLines={1}>
            {format.dateTime(new Date(doc.updated_at), {
              year: "numeric",
              month: "short",
              day: "numeric",
            })}
          </Text>
        ) : undefined
      }
      onClick={() => onOpen(doc)}
      aria-label={t("results.openDocument", {
        title: doc.semantic_identifier || doc.document_id,
      })}
      data-testid={`search-result-${doc.document_id}`}
    />
  );
}
