"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { mutate } from "swr";
import { Button, Card, LinkButton } from "@opal/components";
import {
  SvgPlug,
  SvgRefreshCw,
  SvgSettings,
  SvgTrash,
  SvgUnplug,
} from "@opal/icons";
import { ContentAction, Section, toast } from "@opal/layouts";
import { fetchErrorDetail } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import {
  activateHook,
  deactivateHook,
  deleteHook,
  validateHook,
} from "@/lib/hookExtensions/svc";
import type { Hook, HookPointMeta } from "@/lib/hookExtensions/types";
import { useHookPointText } from "@/views/admin/HooksPage/hookPoints";
import HookFormModal from "@/views/admin/HooksPage/HookFormModal";
import {
  DeleteHookModal,
  DisconnectHookModal,
} from "@/views/admin/HooksPage/HookConfirmModals";
import { HookLogsModal, HookStatus } from "@/views/admin/HooksPage/HookLogs";

type OpenModal = "form" | "disconnect" | "delete" | "logs" | null;

function toastOptions(error: unknown): { description?: string } {
  return { description: fetchErrorDetail(error) ?? undefined };
}

interface HookCardProps {
  spec: HookPointMeta;
  /** The point's hook, or undefined when none is connected. */
  hook: Hook | undefined;
}

export default function HookCard({ spec, hook }: HookCardProps) {
  const t = useTranslations("admin.hooks");
  const hookPointText = useHookPointText();
  const point = hookPointText(spec);
  const [modal, setModal] = useState<OpenModal>(null);
  const [busy, setBusy] = useState(false);

  function close() {
    setModal(null);
  }

  async function refresh() {
    await mutate(SWR_KEYS.hooks);
  }

  async function reconnect(target: Hook) {
    setBusy(true);
    try {
      await activateHook(target.id);
      await refresh();
    } catch (error) {
      toast.error(t("toasts.reconnectFailed.message"), toastOptions(error));
    } finally {
      setBusy(false);
    }
  }

  async function test(target: Hook) {
    setBusy(true);
    try {
      const result = await validateHook(target.id);
      if (result.status === "passed") {
        toast.success(t("toasts.validateSuccess.message"));
      } else {
        toast.error(
          t("toasts.validationFailed.message", { status: result.status }),
          {
            description: result.error_message ?? undefined,
          }
        );
      }
      await refresh();
    } catch (error) {
      toast.error(t("toasts.validateFailed.message"), toastOptions(error));
    } finally {
      setBusy(false);
    }
  }

  async function disconnect(target: Hook, alsoDelete: boolean) {
    try {
      await deactivateHook(target.id);
    } catch (error) {
      toast.error(t("toasts.disconnectFailed.message"), toastOptions(error));
      return;
    }
    if (alsoDelete) {
      try {
        await deleteHook(target.id);
      } catch (error) {
        toast.error(t("toasts.deleteFailed.message"), toastOptions(error));
      }
    }
    await refresh();
    close();
  }

  async function remove(target: Hook) {
    try {
      await deleteHook(target.id);
      await refresh();
      close();
    } catch (error) {
      toast.error(t("toasts.deleteFailed.message"), toastOptions(error));
    }
  }

  const docsLink = spec.docs_url ? (
    <LinkButton href={spec.docs_url} target="_blank">
      {t("docsLink.label")}
    </LinkButton>
  ) : null;

  return (
    <Card border="solid" rounding={4}>
      {hook ? (
        <ContentAction
          font="main-ui-action"
          title={hook.name}
          suffix={
            hook.is_active ? undefined : t("card.disconnectedSuffix.label")
          }
          description={t("card.hookPoint.description", { name: point.name })}
          padding={1}
          rightChildren={
            <Section flexDirection="row" width="fit" gap={0.25}>
              {docsLink}
              {hook.is_active ? (
                <HookStatus hook={hook} onViewLogs={() => setModal("logs")} />
              ) : (
                <Button
                  icon={SvgPlug}
                  prominence="secondary"
                  disabled={busy}
                  onClick={() => void reconnect(hook)}
                >
                  {t("card.reconnectButton.label")}
                </Button>
              )}
              <Button
                icon={SvgSettings}
                prominence="tertiary"
                tooltip={t("card.manage.tooltip")}
                aria-label={t("card.manage.ariaLabel")}
                onClick={() => setModal("form")}
              />
              <Button
                icon={SvgRefreshCw}
                prominence="tertiary"
                tooltip={t("card.test.tooltip")}
                aria-label={t("card.test.ariaLabel")}
                disabled={busy}
                onClick={() => void test(hook)}
              />
              {hook.is_active && (
                <Button
                  icon={SvgUnplug}
                  prominence="tertiary"
                  tooltip={t("card.disconnect.tooltip")}
                  aria-label={t("card.disconnect.ariaLabel")}
                  onClick={() => setModal("disconnect")}
                />
              )}
              <Button
                icon={SvgTrash}
                prominence="tertiary"
                tooltip={t("card.delete.tooltip")}
                aria-label={t("card.delete.ariaLabel")}
                onClick={() => setModal("delete")}
              />
            </Section>
          }
        />
      ) : (
        <ContentAction
          font="main-ui-action"
          title={point.name}
          description={point.description}
          padding={1}
          rightChildren={
            <Section flexDirection="row" width="fit" gap={0.25}>
              {docsLink}
              <Button icon={SvgPlug} onClick={() => setModal("form")}>
                {t("connectButton.label")}
              </Button>
            </Section>
          }
        />
      )}

      {modal === "form" && (
        <HookFormModal spec={spec} existing={hook ?? null} onClose={close} />
      )}
      {hook && modal === "disconnect" && (
        <DisconnectHookModal
          hook={hook}
          onClose={close}
          onDisconnect={(alsoDelete) => disconnect(hook, alsoDelete)}
        />
      )}
      {hook && modal === "delete" && (
        <DeleteHookModal
          hook={hook}
          onClose={close}
          onDelete={() => remove(hook)}
        />
      )}
      {hook && modal === "logs" && (
        <HookLogsModal hook={hook} pointName={point.name} onClose={close} />
      )}
    </Card>
  );
}
