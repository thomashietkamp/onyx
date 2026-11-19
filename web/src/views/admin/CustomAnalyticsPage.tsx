"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import useSWR from "swr";
import {
  Button,
  Card,
  InputPasswordTypeIn,
  InputTextArea,
  MessageCard,
} from "@opal/components";
import { InputVertical, Section, SettingsLayouts, toast } from "@opal/layouts";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { CUSTOM_ANALYTICS_ENABLED } from "@/lib/constants";
import { markdown } from "@opal/utils";
import { errorHandlingFetcher, parseErrorDetail } from "@/lib/fetcher";
import { useSettings } from "@/lib/settings/hooks";
import { SWR_KEYS } from "@/lib/swr-keys";

const route = ADMIN_ROUTES.CUSTOM_ANALYTICS;

/** Renders a message's <i> tags as markdown emphasis. */
function italic(chunks: string): string {
  return `*${chunks}*`;
}

async function saveCustomAnalyticsScript(
  script: string,
  secretKey: string
): Promise<void> {
  const response = await fetch(
    "/api/admin/enterprise-settings/custom-analytics-script",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script, secret_key: secretKey }),
    }
  );
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response, response.statusText));
  }
}

export default function CustomAnalyticsPage() {
  const t = useTranslations("admin.customAnalytics");
  const adminRouteTitle = useAdminRouteTitle();
  const { appName } = useSettings();
  const stored = useSWR<string | null>(
    CUSTOM_ANALYTICS_ENABLED ? SWR_KEYS.customAnalyticsScript : null,
    errorHandlingFetcher
  );
  const [script, setScript] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setScript(stored.data ?? "");
  }, [stored.data]);

  async function handleSave() {
    setSaving(true);
    try {
      await saveCustomAnalyticsScript(script, secretKey);
      await stored.mutate(script.trim());
      setSecretKey("");
      toast.success(t("updateSuccess.message"));
    } catch (error) {
      toast.error(
        t("updateFailed.message", {
          error: error instanceof Error ? error.message : String(error),
        })
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsLayouts.Root loading={stored.isLoading}>
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        description={t("intro.description", { appName })}
        divider
      />
      <SettingsLayouts.Body>
        {!CUSTOM_ANALYTICS_ENABLED ? (
          <MessageCard
            variant="warning"
            title={t("notEnabled.title")}
            description={markdown(
              t.markup("notEnabled.description", { appName, i: italic })
            )}
          />
        ) : (
          <Card border="solid" rounding={4}>
            <Section>
              <InputVertical
                title={t("script.label")}
                description={markdown(
                  t("script.description"),
                  t("script.instructions")
                )}
                withLabel
              >
                <InputTextArea
                  rows={12}
                  value={script}
                  onChange={(event) => setScript(event.target.value)}
                />
              </InputVertical>
              <InputVertical
                title={t("secretKey.label")}
                description={markdown(
                  t.markup("secretKey.description", { appName, i: italic })
                )}
                withLabel
              >
                <InputPasswordTypeIn
                  value={secretKey}
                  onChange={(event) => setSecretKey(event.target.value)}
                />
              </InputVertical>
              <Section alignItems="end" height="auto">
                <Button
                  disabled={saving || !secretKey}
                  onClick={() => void handleSave()}
                >
                  {t("updateButton.label")}
                </Button>
              </Section>
            </Section>
          </Card>
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
