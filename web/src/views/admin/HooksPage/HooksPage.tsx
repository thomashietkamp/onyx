"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { InputTypeIn, MessageCard } from "@opal/components";
import SvgNoResult from "@opal/illustrations/no-result";
import { IllustrationContent, Section, SettingsLayouts } from "@opal/layouts";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { useSettings } from "@/lib/settings/hooks";
import { useHookSpecs, useHooks } from "@/lib/hookExtensions/hooks";
import { useHookPointText } from "@/views/admin/HooksPage/hookPoints";
import HookCard from "@/views/admin/HooksPage/HookCard";

const route = ADMIN_ROUTES.HOOKS;

export default function HooksPage() {
  const t = useTranslations("admin.hooks");
  const adminRouteTitle = useAdminRouteTitle();
  const settings = useSettings();
  const enabled = settings.hooks_enabled === true;
  const specs = useHookSpecs(enabled);
  const hooks = useHooks(enabled);
  const hookPointText = useHookPointText();
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const allSpecs = specs.data ?? [];
  const visible = allSpecs.filter((spec) => {
    if (!needle) return true;
    const text = hookPointText(spec);
    const hook = hooks.data?.find((h) => h.hook_point === spec.hook_point);
    return [text.name, text.description, hook?.name ?? ""].some((value) =>
      value.toLowerCase().includes(needle)
    );
  });

  function body() {
    if (!enabled) {
      return (
        <MessageCard variant="warning" title={t("page.notEnabled.message")} />
      );
    }
    if (specs.error) {
      return (
        <MessageCard
          variant="error"
          title={t("page.loadSpecsFailed.message")}
        />
      );
    }
    if (hooks.error) {
      return (
        <MessageCard
          variant="error"
          title={t("page.loadHooksFailed.message")}
        />
      );
    }
    if (allSpecs.length === 0) {
      return (
        <IllustrationContent
          illustration={SvgNoResult}
          title={t("page.empty.title")}
        />
      );
    }
    return (
      <Section gap={0.5} alignItems="stretch">
        <InputTypeIn
          icon="search"
          value={query}
          placeholder={t("page.search.placeholder")}
          onChange={(event) => setQuery(event.target.value)}
        />
        {visible.length === 0 ? (
          <IllustrationContent
            illustration={SvgNoResult}
            title={t("page.emptySearch.title")}
            description={t("page.emptySearch.description")}
          />
        ) : (
          visible.map((spec) => (
            <HookCard
              key={spec.hook_point}
              spec={spec}
              hook={hooks.data?.find((h) => h.hook_point === spec.hook_point)}
            />
          ))
        )}
      </Section>
    );
  }

  return (
    <SettingsLayouts.Root
      loading={settings.isLoading || specs.isLoading || hooks.isLoading}
    >
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        description={t("page.description", { appName: settings.appName })}
        divider
      />
      <SettingsLayouts.Body>{body()}</SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
