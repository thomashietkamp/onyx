"use client";

import { InputDateRangePicker } from "@opal/components";
import { SettingsLayouts } from "@opal/layouts";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { useTimeRange } from "@/lib/usage/hooks";
import PerUserUsagePanel from "@/views/admin/PerUserUsagePanel";
import SystemUsagePanel from "@/views/admin/SystemUsagePanel";

const route = ADMIN_ROUTES.USAGE;

/** Admin > Usage: spend and tokens per user, and for system work. */
export default function UsagePage() {
  const adminRouteTitle = useAdminRouteTitle();
  const [timeRange, setTimeRange] = useTimeRange();

  return (
    <SettingsLayouts.Root width="lg">
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
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
        <PerUserUsagePanel timeRange={timeRange} />
        <SystemUsagePanel timeRange={timeRange} />
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
