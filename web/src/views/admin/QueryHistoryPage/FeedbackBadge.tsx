"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@opal/components";
import type { QAFeedbackType } from "@/lib/queryHistory/types";

interface FeedbackBadgeProps {
  feedbackType: QAFeedbackType | null;
}

export default function FeedbackBadge({ feedbackType }: FeedbackBadgeProps) {
  const t = useTranslations("admin.queryHistory.feedback");
  switch (feedbackType) {
    case "like":
      return <Badge title={t("like.label")} color="success" size={5} />;
    case "dislike":
      return <Badge title={t("dislike.label")} color="error" size={5} />;
    case "mixed":
      return <Badge title={t("mixed.label")} color="warning" size={5} />;
    default:
      return <Badge title={t("na.label")} size={5} />;
  }
}
