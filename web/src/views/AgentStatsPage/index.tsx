"use client";

import { useFormatter, useTranslations } from "next-intl";
import { Card, InputDateRangePicker, Text } from "@opal/components";
import { SvgActivity } from "@opal/icons";
import { CardGrid, Section, SettingsLayouts } from "@opal/layouts";
import { FetchError } from "@/lib/fetcher";
import { useAgentStats, useTimeRange } from "@/lib/usage/hooks";
import {
  AnalyticsChart,
  chartSeries,
  resolveChartState,
} from "@/sections/usage/AnalyticsChart";

interface StatTileProps {
  label: string;
  value: number;
}

function StatTile({ label, value }: StatTileProps) {
  const format = useFormatter();
  return (
    <Card border="solid" rounding={4} padding={4}>
      <Section
        flexDirection="column"
        alignItems="start"
        gap={0.25}
        width="full"
        height="fit"
      >
        <Text as="p" font="secondary-body" color="text-03">
          {label}
        </Text>
        <Text as="p" font="heading-section">
          {format.number(value)}
        </Text>
      </Section>
    </Card>
  );
}

export interface AgentStatsPageProps {
  agentId: number;
  agentName: string;
}

export default function AgentStatsPage({
  agentId,
  agentName,
}: AgentStatsPageProps) {
  const t = useTranslations("admin.agentStats");
  const [timeRange, setTimeRange] = useTimeRange();
  const { stats, error, isLoading } = useAgentStats(agentId, timeRange);

  const errorMessage =
    error instanceof FetchError && error.status === 403
      ? t("permissionDenied.message")
      : error
        ? t("fetchFailed.message")
        : undefined;

  const series = [
    chartSeries(
      t("totalMessages.label"),
      stats?.daily_stats,
      (entry) => entry.total_messages
    ),
    chartSeries(
      t("totalUniqueUsers.label"),
      stats?.daily_stats,
      (entry) => entry.total_unique_users
    ),
  ];

  return (
    <SettingsLayouts.Root width="lg" error={errorMessage ?? false}>
      <SettingsLayouts.Header
        icon={SvgActivity}
        title={agentName}
        divider
        actions={[
          <InputDateRangePicker
            key="primary"
            value={timeRange}
            onValueChange={(range) =>
              setTimeRange((previous) =>
                range
                  ? { ...range, selectValue: previous.selectValue }
                  : previous
              )
            }
          />,
        ]}
      />
      <SettingsLayouts.Body>
        <CardGrid>
          <StatTile
            label={t("totalMessages.label")}
            value={stats?.total_messages ?? 0}
          />
          <StatTile
            label={t("totalUniqueUsers.label")}
            value={stats?.total_unique_users ?? 0}
          />
        </CardGrid>
        <AnalyticsChart
          title={t("chart.title")}
          description={t("chart.description")}
          timeRange={timeRange}
          allowDecimals={false}
          state={resolveChartState({
            isLoading,
            error,
            errorMessage: t("fetchFailed.message"),
            emptyMessage: t("empty.message"),
            series,
          })}
        />
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
