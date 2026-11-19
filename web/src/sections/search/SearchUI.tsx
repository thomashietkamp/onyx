"use client";

import type { MinimalOnyxDocument } from "@/lib/search/types";

export interface SearchUIProps {
  onDocumentClick: (doc: MinimalOnyxDocument) => void;
}

/**
 * Search mode is not in this build yet. The query controller never routes to
 * search, so this view never shows.
 */
export default function SearchUI(_props: SearchUIProps) {
  return null;
}
