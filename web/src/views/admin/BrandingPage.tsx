"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import useSWR, { mutate } from "swr";
import {
  Button,
  Card,
  InputImage,
  InputSingleSelect,
  InputSwitch,
  InputTextArea,
  InputTypeIn,
  type SelectOption,
} from "@opal/components";
import {
  InputHorizontal,
  InputVertical,
  Section,
  SettingsLayouts,
  toast,
} from "@opal/layouts";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import { useSettings } from "@/lib/settings/hooks";
import type { EnterpriseSettings } from "@/lib/settings/types";
import {
  type BrandingLimits,
  type BrandingSettings,
  deleteBrandingLogo,
  saveBrandingSettings,
  toBrandingSettings,
  uploadBrandingLogo,
} from "@/lib/settings/brandingSvc";

const route = ADMIN_ROUTES.THEME;

type TextField =
  | "application_name"
  | "custom_greeting_message"
  | "custom_header_content"
  | "custom_lower_disclaimer_content"
  | "custom_login_subtitle"
  | "custom_popup_header"
  | "custom_popup_content"
  | "consent_screen_prompt"
  | "custom_help_link_url"
  | "custom_help_link_label";

const TEXT_FIELDS: readonly TextField[] = [
  "application_name",
  "custom_greeting_message",
  "custom_header_content",
  "custom_lower_disclaimer_content",
  "custom_login_subtitle",
  "custom_popup_header",
  "custom_popup_content",
  "consent_screen_prompt",
  "custom_help_link_url",
  "custom_help_link_label",
];

type DisplayStyle = NonNullable<EnterpriseSettings["logo_display_style"]>;

const DISPLAY_STYLES: readonly DisplayStyle[] = [
  "logo_and_name",
  "logo_only",
  "name_only",
];

function isDisplayStyle(value: string): value is DisplayStyle {
  return DISPLAY_STYLES.some((style) => style === value);
}

type FieldErrors = Partial<Record<TextField, string>>;

function isWebUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

interface BrandingFormProps {
  stored: EnterpriseSettings;
  limits: BrandingLimits;
}

function BrandingForm({ stored, limits }: BrandingFormProps) {
  const t = useTranslations("admin.theme");
  const tInputSelect = useTranslations("common.inputSelect");
  const { logoUrl } = useSettings();
  const [draft, setDraft] = useState<BrandingSettings>(() =>
    toBrandingSettings(stored)
  );
  const [saving, setSaving] = useState(false);
  const [logoBusy, setLogoBusy] = useState(false);

  function text(field: TextField): string {
    return draft[field] ?? "";
  }

  function setText(field: TextField, value: string) {
    setDraft((previous) => ({ ...previous, [field]: value || null }));
  }

  function setFlag(
    field: "show_first_visit_notice" | "enable_consent_screen",
    value: boolean
  ) {
    setDraft((previous) => ({ ...previous, [field]: value }));
  }

  function validate(): FieldErrors {
    const errors: FieldErrors = {};
    for (const field of TEXT_FIELDS) {
      const limit = limits.field_max_lengths[field];
      if (limit !== undefined && text(field).length > limit) {
        errors[field] = t("page.validation.maxChars", { count: limit });
      }
    }
    const url = text("custom_help_link_url").trim();
    if (url && !isWebUrl(url)) {
      errors.custom_help_link_url = t("page.validation.invalidUrl");
    }
    if (!url && text("custom_help_link_label").trim()) {
      errors.custom_help_link_url = t("page.validation.urlRequiredWithLabel");
    }
    if (draft.show_first_visit_notice) {
      if (!text("custom_popup_header").trim()) {
        errors.custom_popup_header = t("page.validation.noticeHeaderRequired");
      }
      if (!text("custom_popup_content").trim()) {
        errors.custom_popup_content = t(
          "page.validation.noticeContentRequired"
        );
      }
    }
    if (draft.enable_consent_screen && !text("consent_screen_prompt").trim()) {
      errors.consent_screen_prompt = t("page.validation.consentPromptRequired");
    }
    return errors;
  }

  const errors = validate();
  const hasErrors = Object.keys(errors).length > 0;

  async function refresh(next: EnterpriseSettings) {
    // A new object also gives every logo a new cache-buster.
    await mutate(SWR_KEYS.enterpriseSettings, next, { revalidate: false });
  }

  async function handleSave() {
    if (hasErrors) return;
    setSaving(true);
    try {
      await refresh(await saveBrandingSettings(draft));
      toast.success(t("page.saveSuccess.message"));
    } catch (error) {
      toast.error(
        t("page.settingsSaveFailed.message", {
          error: error instanceof Error ? error.message : String(error),
        })
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleLogo(change: () => Promise<EnterpriseSettings>) {
    setLogoBusy(true);
    try {
      await refresh(await change());
    } catch (error) {
      toast.error(
        t("page.logoUploadFailed.message", {
          error: error instanceof Error ? error.message : String(error),
        })
      );
    } finally {
      setLogoBusy(false);
    }
  }

  const displayStyleOptions: SelectOption[] = [
    { value: "logo_and_name", title: t("logoStyle.both.label") },
    { value: "logo_only", title: t("logoStyle.logoOnly.label") },
    { value: "name_only", title: t("logoStyle.nameOnly.label") },
  ];

  function textInput(field: TextField, placeholder?: string) {
    return (
      <InputTypeIn
        value={text(field)}
        placeholder={placeholder}
        variant={errors[field] ? "error" : "primary"}
        onChange={(event) => setText(field, event.target.value)}
      />
    );
  }

  function textArea(field: TextField) {
    return (
      <InputTextArea
        rows={3}
        value={text(field)}
        placeholder={t("markdownContent.placeholder")}
        variant={errors[field] ? "error" : "primary"}
        onChange={(event) => setText(field, event.target.value)}
      />
    );
  }

  return (
    <SettingsLayouts.Body>
      <Card border="solid" rounding={4}>
        <Section>
          <InputVertical
            title={t("appName.label")}
            description={t("appName.description")}
            error={errors.application_name}
            withLabel
          >
            {textInput("application_name")}
          </InputVertical>

          <InputVertical title={t("logo.label")}>
            <InputImage
              src={logoUrl ?? undefined}
              alt={t("preview.logo.alt")}
              disabled={logoBusy}
              size={80}
              onDrop={(file) =>
                void handleLogo(() => uploadBrandingLogo("logo", file))
              }
              onDropRejected={(reason) =>
                toast.error(
                  t("page.logoUploadFailed.message", { error: reason })
                )
              }
              onRemove={
                stored.use_custom_logo
                  ? () => void handleLogo(() => deleteBrandingLogo("logo"))
                  : undefined
              }
            />
          </InputVertical>

          <InputVertical
            title={t("logoStyle.label")}
            description={t("logoStyle.description")}
          >
            <InputSingleSelect
              value={draft.logo_display_style ?? "logo_and_name"}
              onValueChange={(value) => {
                if (!isDisplayStyle(value)) return;
                setDraft((previous) => ({
                  ...previous,
                  logo_display_style: value,
                }));
              }}
              options={displayStyleOptions}
              placeholder={tInputSelect("placeholder.fallback")}
            />
          </InputVertical>
        </Section>
      </Card>

      <Card border="solid" rounding={4}>
        <Section>
          <InputVertical
            title={t("greeting.label")}
            description={t("greeting.description")}
            error={errors.custom_greeting_message}
            withLabel
          >
            {textInput(
              "custom_greeting_message",
              t("preview.greeting.placeholder")
            )}
          </InputVertical>

          <InputVertical
            title={t("chatHeader.label")}
            error={errors.custom_header_content}
            withLabel
          >
            {textInput("custom_header_content")}
          </InputVertical>

          <InputVertical
            title={t("chatFooter.label")}
            description={t("chatFooter.description")}
            error={errors.custom_lower_disclaimer_content}
            withLabel
          >
            {textArea("custom_lower_disclaimer_content")}
          </InputVertical>

          <InputVertical
            title={t("loginSubtitle.label")}
            description={t("loginSubtitle.description")}
            error={errors.custom_login_subtitle}
            withLabel
          >
            {textInput("custom_login_subtitle")}
          </InputVertical>
        </Section>
      </Card>

      <Card border="solid" rounding={4}>
        <Section>
          <InputHorizontal
            title={t("firstVisit.label")}
            description={t("firstVisit.description")}
            withLabel
          >
            <InputSwitch
              checked={draft.show_first_visit_notice ?? false}
              onCheckedChange={(checked) =>
                setFlag("show_first_visit_notice", checked)
              }
            />
          </InputHorizontal>

          {draft.show_first_visit_notice && (
            <>
              <InputVertical
                title={t("noticeHeader.label")}
                error={errors.custom_popup_header}
                withLabel
              >
                {textInput("custom_popup_header")}
              </InputVertical>
              <InputVertical
                title={t("noticeContent.label")}
                error={errors.custom_popup_content}
                withLabel
              >
                {textArea("custom_popup_content")}
              </InputVertical>
            </>
          )}

          <InputHorizontal
            title={t("consent.label")}
            description={t("consent.description")}
            withLabel
          >
            <InputSwitch
              checked={draft.enable_consent_screen ?? false}
              onCheckedChange={(checked) =>
                setFlag("enable_consent_screen", checked)
              }
            />
          </InputHorizontal>

          {draft.enable_consent_screen && (
            <InputVertical
              title={t("consentPrompt.label")}
              error={errors.consent_screen_prompt}
              withLabel
            >
              {textArea("consent_screen_prompt")}
            </InputVertical>
          )}
        </Section>
      </Card>

      <Card border="solid" rounding={4}>
        <Section>
          <InputVertical
            title={t("helpLink.label")}
            description={t("helpLink.description")}
            error={errors.custom_help_link_url}
            withLabel
          >
            {textInput("custom_help_link_url", t("placeholders.helpUrl"))}
          </InputVertical>
          <InputVertical
            title={t("helpLinkLabel.label")}
            error={errors.custom_help_link_label}
            withLabel
          >
            {textInput(
              "custom_help_link_label",
              t("helpLinkLabel.placeholder")
            )}
          </InputVertical>
        </Section>
      </Card>

      <Section flexDirection="row" justifyContent="end" height="fit">
        <Button
          disabled={saving || hasErrors}
          onClick={() => void handleSave()}
        >
          {saving ? t("page.applying.label") : t("page.applyButton.label")}
        </Button>
      </Section>
    </SettingsLayouts.Body>
  );
}

export default function BrandingPage() {
  const t = useTranslations("admin.theme");
  const adminRouteTitle = useAdminRouteTitle();
  const { data: stored } = useSWR<EnterpriseSettings>(
    SWR_KEYS.enterpriseSettings,
    errorHandlingFetcher
  );
  const { data: limits } = useSWR<BrandingLimits>(
    SWR_KEYS.brandingLimits,
    errorHandlingFetcher
  );

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        description={t("page.description")}
        divider
      />
      {stored && limits ? (
        <BrandingForm stored={stored} limits={limits} />
      ) : (
        <SettingsLayouts.Body />
      )}
    </SettingsLayouts.Root>
  );
}
