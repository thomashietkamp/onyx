"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { mutate } from "swr";
import {
  BasicModalFooter,
  Button,
  InputNumber,
  InputPasswordTypeIn,
  InputSingleSelect,
  InputTypeIn,
  MessageCard,
  Modal,
  type SelectOption,
} from "@opal/components";
import { SvgRevert } from "@opal/icons";
import { InputVertical, Section, toast } from "@opal/layouts";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { useSettings } from "@/lib/settings/hooks";
import { SWR_KEYS } from "@/lib/swr-keys";
import { createHook, updateHook } from "@/lib/hookExtensions/svc";
import type {
  Hook,
  HookFailStrategy,
  HookPointMeta,
  HookUpdateRequest,
} from "@/lib/hookExtensions/types";
import {
  MAX_HOOK_TIMEOUT_SECONDS,
  formErrorKeyForError,
  type HookFormErrorKey,
} from "@/lib/hookExtensions/utils";
import { useHookPointText } from "@/views/admin/HooksPage/hookPoints";

function isFailStrategy(value: string): value is HookFailStrategy {
  return value === "hard" || value === "soft";
}

interface HookFormModalProps {
  spec: HookPointMeta;
  /** Null to create a hook at `spec`'s hook point. */
  existing: Hook | null;
  onClose: () => void;
}

export default function HookFormModal({
  spec,
  existing,
  onClose,
}: HookFormModalProps) {
  const t = useTranslations("admin.hooks");
  const { appName } = useSettings();
  const hookPointText = useHookPointText();
  const point = hookPointText(spec);

  const [name, setName] = useState(existing?.name ?? "");
  const [endpointUrl, setEndpointUrl] = useState(existing?.endpoint_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [failStrategy, setFailStrategy] = useState<HookFailStrategy>(
    existing?.fail_strategy ?? spec.default_fail_strategy
  );
  const [timeout, setTimeoutSeconds] = useState<number | null>(
    existing?.timeout_seconds ?? spec.default_timeout_seconds
  );
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<HookFormErrorKey | null>(null);

  const nameError = !name.trim() ? t("form.validation.nameRequired") : null;
  const endpointError = !endpointUrl.trim()
    ? t("form.validation.endpointRequired")
    : null;
  const apiKeyError =
    !existing && !apiKey.trim() ? t("form.validation.apiKeyRequired") : null;
  const timeoutError =
    timeout === null
      ? t("form.validation.timeoutRequired")
      : timeout <= 0 || timeout > MAX_HOOK_TIMEOUT_SECONDS
        ? t("form.validation.timeoutRange", { max: MAX_HOOK_TIMEOUT_SECONDS })
        : null;
  const invalid =
    nameError !== null ||
    endpointError !== null ||
    apiKeyError !== null ||
    timeoutError !== null;

  function defaultSuffix(strategy: HookFailStrategy): string | undefined {
    return strategy === spec.default_fail_strategy
      ? t("form.failStrategy.default.label")
      : undefined;
  }

  const strategyOptions: SelectOption[] = [
    {
      value: "hard",
      title: t("form.failStrategy.hard.label"),
      suffix: defaultSuffix("hard"),
      description: spec.fail_hard_description,
    },
    {
      value: "soft",
      title: t("form.failStrategy.soft.label"),
      suffix: defaultSuffix("soft"),
      description: t("form.softStrategy.description", { appName }),
    },
  ];

  /** Only the fields that changed; the API rejects an empty update. */
  function changedFields(hook: Hook, seconds: number): HookUpdateRequest {
    const changes: HookUpdateRequest = {};
    if (name.trim() !== hook.name) changes.name = name.trim();
    if (endpointUrl.trim() !== (hook.endpoint_url ?? "")) {
      changes.endpoint_url = endpointUrl.trim();
    }
    if (apiKey.trim()) changes.api_key = apiKey.trim();
    if (failStrategy !== hook.fail_strategy) {
      changes.fail_strategy = failStrategy;
    }
    if (seconds !== hook.timeout_seconds) changes.timeout_seconds = seconds;
    return changes;
  }

  async function save() {
    setTouched(true);
    if (invalid || timeout === null) return;
    setSaving(true);
    setSaveError(null);
    try {
      if (existing) {
        const changes = changedFields(existing, timeout);
        if (Object.keys(changes).length > 0) {
          await updateHook(existing.id, changes);
          toast.success(t("form.toasts.updated.message"));
        }
      } else {
        await createHook({
          name: name.trim(),
          hook_point: spec.hook_point,
          endpoint_url: endpointUrl.trim(),
          api_key: apiKey.trim(),
          fail_strategy: failStrategy,
          timeout_seconds: timeout,
        });
        toast.success(t("form.toasts.created.message"));
      }
      await mutate(SWR_KEYS.hooks);
      onClose();
    } catch (error) {
      setSaveError(formErrorKeyForError(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open onOpenChange={() => !saving && onClose()}>
      <Modal.Content width="md" height="fit">
        <Modal.Header
          icon={ADMIN_ROUTES.HOOKS.icon}
          title={
            existing ? t("form.editHeader.title") : t("form.createHeader.title")
          }
          description={
            existing ? undefined : t("form.createHeader.description")
          }
          onClose={saving ? undefined : onClose}
        />
        <Modal.Body>
          <Section gap={1} alignItems="stretch">
            <InputVertical title={t("form.hookPoint.label")}>
              <InputTypeIn value={point.name} variant="readOnly" readOnly />
            </InputVertical>
            <InputVertical
              title={t("form.name.title")}
              error={touched ? (nameError ?? undefined) : undefined}
              withLabel
            >
              <InputTypeIn
                value={name}
                placeholder={t("form.name.placeholder")}
                onChange={(event) => setName(event.target.value)}
              />
            </InputVertical>
            <InputVertical
              title={t("form.endpoint.title")}
              description={t("form.endpoint.description")}
              error={touched ? (endpointError ?? undefined) : undefined}
              withLabel
            >
              <InputTypeIn
                value={endpointUrl}
                placeholder={t("placeholders.endpointUrl")}
                onChange={(event) => setEndpointUrl(event.target.value)}
              />
            </InputVertical>
            <InputVertical
              title={t("form.apiKey.title")}
              description={t("form.apiKey.description", { appName })}
              error={touched ? (apiKeyError ?? undefined) : undefined}
              withLabel
            >
              <InputPasswordTypeIn
                value={apiKey}
                placeholder={
                  existing
                    ? t("form.apiKey.keepCurrent.placeholder")
                    : undefined
                }
                onChange={(event) => setApiKey(event.target.value)}
              />
            </InputVertical>
            <InputVertical title={t("form.failStrategy.title")}>
              <InputSingleSelect
                value={failStrategy}
                onValueChange={(value) => {
                  if (isFailStrategy(value)) setFailStrategy(value);
                }}
                options={strategyOptions}
                placeholder={t("form.failStrategy.placeholder")}
              />
            </InputVertical>
            <InputVertical
              title={t("form.timeout.title")}
              suffix={t("form.timeout.suffix")}
              description={t("form.timeout.description", {
                appName,
                max: MAX_HOOK_TIMEOUT_SECONDS,
              })}
              error={touched ? (timeoutError ?? undefined) : undefined}
              rightChildren={
                <Button
                  icon={SvgRevert}
                  prominence="tertiary"
                  size="sm"
                  tooltip={t("form.timeout.revert.tooltip")}
                  disabled={timeout === spec.default_timeout_seconds}
                  onClick={() =>
                    setTimeoutSeconds(spec.default_timeout_seconds)
                  }
                />
              }
            >
              <InputNumber
                value={timeout}
                onChange={setTimeoutSeconds}
                min={1}
                max={MAX_HOOK_TIMEOUT_SECONDS}
              />
            </InputVertical>
            {saveError && (
              <MessageCard
                variant="error"
                title={t(`form.errors.${saveError}`)}
              />
            )}
          </Section>
        </Modal.Body>
        <Modal.Footer>
          <BasicModalFooter
            cancel={
              <Button
                prominence="secondary"
                disabled={saving}
                onClick={onClose}
              >
                {t("modals.cancel.label")}
              </Button>
            }
            submit={
              <Button
                disabled={saving || (touched && invalid)}
                onClick={() => void save()}
              >
                {saving
                  ? t("form.verifying.label")
                  : existing
                    ? t("form.save.label")
                    : t("connectButton.label")}
              </Button>
            }
          />
        </Modal.Footer>
      </Modal.Content>
    </Modal>
  );
}
