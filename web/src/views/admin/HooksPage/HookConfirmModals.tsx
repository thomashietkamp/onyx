"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Text } from "@opal/components";
import { SvgTrash, SvgUnplug } from "@opal/icons";
import { ConfirmationModalLayout, Section } from "@opal/layouts";
import { escapeMarkdown, markdown } from "@opal/utils";
import { useSettings } from "@/lib/settings/hooks";
import type { Hook } from "@/lib/hookExtensions/types";

interface DisconnectHookModalProps {
  hook: Hook;
  onClose: () => void;
  /** Resolves once the action finished; the caller shows any error. */
  onDisconnect: (alsoDelete: boolean) => Promise<void>;
}

export function DisconnectHookModal({
  hook,
  onClose,
  onDisconnect,
}: DisconnectHookModalProps) {
  const t = useTranslations("admin.hooks.disconnectModal");
  const { appName } = useSettings();
  const [busy, setBusy] = useState(false);
  const name = escapeMarkdown(hook.name);

  async function run(alsoDelete: boolean) {
    setBusy(true);
    try {
      await onDisconnect(alsoDelete);
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConfirmationModalLayout
      icon={SvgUnplug}
      title={markdown(t("header.title", { name }))}
      onClose={busy ? undefined : onClose}
      submit={
        <>
          <Button
            variant="danger"
            prominence="secondary"
            disabled={busy}
            onClick={() => void run(true)}
          >
            {t("disconnectAndDelete.label")}
          </Button>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => void run(false)}
          >
            {t("disconnect.label")}
          </Button>
        </>
      }
    >
      <Section gap={0.5} alignItems="stretch">
        <Text as="p" color="text-03">
          {markdown(t("body.description", { appName, name }))}
        </Text>
        <Text as="p" color="text-03">
          {t("deleteNote.description")}
        </Text>
      </Section>
    </ConfirmationModalLayout>
  );
}

interface DeleteHookModalProps {
  hook: Hook;
  onClose: () => void;
  onDelete: () => Promise<void>;
}

export function DeleteHookModal({
  hook,
  onClose,
  onDelete,
}: DeleteHookModalProps) {
  const t = useTranslations("admin.hooks.deleteModal");
  const [busy, setBusy] = useState(false);
  const name = escapeMarkdown(hook.name);

  async function run() {
    setBusy(true);
    try {
      await onDelete();
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConfirmationModalLayout
      icon={SvgTrash}
      color="error"
      title={markdown(t("header.title", { name }))}
      onClose={busy ? undefined : onClose}
      submit={
        <Button variant="danger" disabled={busy} onClick={() => void run()}>
          {t("delete.label")}
        </Button>
      }
    >
      <Section gap={0.5} alignItems="stretch">
        <Text as="p" color="text-03">
          {markdown(t("body.description", { name }))}
        </Text>
        <Text as="p" color="text-03">
          {t("permanent.description")}
        </Text>
      </Section>
    </ConfirmationModalLayout>
  );
}
