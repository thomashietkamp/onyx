"use client";

import { useState } from "react";
import { useFormatter, useTranslations } from "next-intl";
import { mutate } from "swr";
import {
  Button,
  InputDateRangePicker,
  InputSingleSelect,
  MessageCard,
  Table,
  Text,
  type SelectOption,
  type TableColumn,
} from "@opal/components";
import { SvgHistory, SvgUploadCloud } from "@opal/icons";
import { InputVertical, Section, SettingsLayouts, toast } from "@opal/layouts";
import { useNavigation } from "@opal/navigation";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import {
  convertDateToEndOfDay,
  convertDateToStartOfDay,
} from "@/lib/dateUtils";
import { SWR_KEYS } from "@/lib/swr-keys";
import { useTimeRange } from "@/lib/usage/hooks";
import { useChatSessionHistory } from "@/lib/queryHistory/hooks";
import { startQueryHistoryExport } from "@/lib/queryHistory/svc";
import type {
  ChatSessionMinimal,
  QAFeedbackType,
} from "@/lib/queryHistory/types";
import { previewText, toFeedbackType } from "@/lib/queryHistory/utils";
import ExportsModal from "@/views/admin/QueryHistoryPage/ExportsModal";
import FeedbackBadge from "@/views/admin/QueryHistoryPage/FeedbackBadge";

const route = ADMIN_ROUTES.QUERY_HISTORY;
const ANY = "any";
const DEFAULT_PAGE_SIZE = 10;

export default function QueryHistoryPage() {
  const t = useTranslations("admin.queryHistory");
  const format = useFormatter();
  const navigation = useNavigation();
  const adminRouteTitle = useAdminRouteTitle();
  const [timeRange, setTimeRange] = useTimeRange();
  const [feedbackType, setFeedbackType] = useState<QAFeedbackType | null>(null);
  const [pageNum, setPageNum] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [exportsOpen, setExportsOpen] = useState(false);
  const [exporting, setExporting] = useState(false);

  const start = convertDateToStartOfDay(timeRange.from) ?? null;
  const end = convertDateToEndOfDay(timeRange.to) ?? null;
  const history = useChatSessionHistory({
    feedbackType,
    start,
    end,
    pageNum,
    pageSize,
  });

  const feedbackOptions: SelectOption[] = [
    { value: ANY, title: t("filters.any.label") },
    { value: "like", title: t("feedback.like.label") },
    { value: "dislike", title: t("feedback.dislike.label") },
    { value: "mixed", title: t("feedback.mixed.label") },
  ];

  async function kickoffExport() {
    setExporting(true);
    try {
      await startQueryHistoryExport(start, end);
      await mutate(SWR_KEYS.adminQueryHistoryExports);
      toast.success(
        t("export.generating.message", { button: t("previousExports.label") })
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      setExporting(false);
    }
  }

  const columns: TableColumn<ChatSessionMinimal>[] = [
    {
      kind: "data",
      field: "first_user_message",
      title: t("table.firstUserMessage.header"),
      weight: 25,
      sortable: false,
      cell: (value) => (
        <Text font="main-ui-body" maxLines={3} overflowTooltip>
          {previewText(value)}
        </Text>
      ),
    },
    {
      kind: "data",
      field: "first_ai_message",
      title: t("table.firstAiResponse.header"),
      weight: 35,
      sortable: false,
      cell: (value) => (
        <Text font="main-ui-body" maxLines={3} overflowTooltip>
          {previewText(value)}
        </Text>
      ),
    },
    {
      kind: "data",
      field: "feedback_type",
      title: t("table.feedback.header"),
      weight: 10,
      sortable: false,
      cell: (value) => <FeedbackBadge feedbackType={value} />,
    },
    {
      kind: "data",
      field: "user_email",
      title: t("table.user.header"),
      weight: 15,
      sortable: false,
      cell: (value) => (
        <Text font="main-ui-body" maxLines={1} overflowTooltip>
          {value ?? "-"}
        </Text>
      ),
    },
    {
      kind: "data",
      field: "persona_name",
      title: t("table.persona.header"),
      weight: 10,
      sortable: false,
      cell: (value) => (
        <Text font="main-ui-body" maxLines={1} overflowTooltip>
          {value ?? t("assistant.unknown.label")}
        </Text>
      ),
    },
    {
      kind: "data",
      field: "time_created",
      title: t("table.date.header"),
      weight: 10,
      sortable: false,
      cell: (value) => (
        <Text font="secondary-body" color="text-03">
          {format.dateTime(new Date(value), {
            dateStyle: "short",
            timeStyle: "short",
          })}
        </Text>
      ),
    },
  ];

  return (
    <SettingsLayouts.Root width="lg">
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        divider
        actions={[
          <Button
            key="kickoff"
            icon={SvgUploadCloud}
            disabled={exporting}
            onClick={() => void kickoffExport()}
          >
            {t("export.kickoff.label")}
          </Button>,
          <Button
            key="exports"
            icon={SvgHistory}
            prominence="secondary"
            onClick={() => setExportsOpen(true)}
          >
            {t("previousExports.label")}
          </Button>,
        ]}
      />
      <SettingsLayouts.Body>
        <Section
          flexDirection="row"
          justifyContent="start"
          alignItems="end"
          gap={1}
        >
          <InputVertical title={t("filters.feedbackType.label")} withLabel>
            <InputSingleSelect
              value={feedbackType ?? ANY}
              defaultOption={ANY}
              placeholder={t("filters.feedbackType.label")}
              options={feedbackOptions}
              onValueChange={(value) => {
                setFeedbackType(toFeedbackType(value));
                setPageNum(0);
              }}
            />
          </InputVertical>
          <InputDateRangePicker
            value={timeRange}
            onValueChange={(range) => {
              setTimeRange((previous) =>
                range
                  ? { ...range, selectValue: previous.selectValue }
                  : previous
              );
              setPageNum(0);
            }}
          />
        </Section>
        {history.error ? (
          <MessageCard variant="error" title={t("table.fetchError.title")} />
        ) : (
          <Table
            label={adminRouteTitle(route)}
            items={history.data?.items ?? []}
            columns={columns}
            getRowId={(row) => row.id}
            pageSize={pageSize}
            footer={{}}
            onRowClick={(row) => navigation.go(`${route.path}/${row.id}`)}
            getRowLabel={(row) => previewText(row.first_user_message, 80)}
            serverSide={{
              totalItems: history.data?.total_items ?? 0,
              isLoading: history.isLoading,
              onSortingChange: () => {},
              onPaginationChange: (nextPage, nextSize) => {
                setPageNum(nextPage);
                setPageSize(nextSize);
              },
              onQueryChange: () => {},
            }}
          />
        )}
        {exportsOpen && <ExportsModal onClose={() => setExportsOpen(false)} />}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
