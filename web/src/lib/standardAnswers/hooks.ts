import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type { StandardAnswer, StandardAnswerCategory } from "@/lib/types";
import type { StandardAnswerCategoryResponse } from "@/components/standardAnswers/getStandardAnswerCategoriesIfEE";

export function useStandardAnswerCategories() {
  return useSWR<StandardAnswerCategory[]>(
    SWR_KEYS.standardAnswerCategories,
    errorHandlingFetcher
  );
}

export function useStandardAnswers() {
  return useSWR<StandardAnswer[]>(
    SWR_KEYS.standardAnswers,
    errorHandlingFetcher
  );
}

/** The category picker's input on the Slack channel form. */
export function useStandardAnswerCategoryResponse(): {
  response: StandardAnswerCategoryResponse;
  isLoading: boolean;
} {
  const { data, error, isLoading } = useStandardAnswerCategories();
  return {
    isLoading,
    response: {
      paidEnterpriseFeaturesEnabled: true,
      categories: data ?? [],
      error: error ? { message: String(error) } : undefined,
    },
  };
}
