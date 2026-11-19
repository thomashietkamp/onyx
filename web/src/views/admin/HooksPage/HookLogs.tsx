"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  Badge,
  Button,
  CodePanel,
  MessageCard,
  Modal,
  Popover,
  Text,
} from "@opal/components";
import { SvgCopy, SvgDownload } from "@opal/icons";
import { Content, Section } from "@opal/layouts";
import { LineLoader } from "@opal/loaders";
import { copyText } from "@opal/utils";
import { downloadFile } from "@/lib/download";
import { useHookExecutionLogs } from "@/lib/hookExtensions/hooks";
import type { Hook, HookExecutionLog } from "@/lib/hookExtensions/types";
import {
  formatHookLogLine,
  formatHookLogs,
  groupHookLogs,
} from "@/lib/hookExtensions/utils";

/** Lines the status popover shows before "View More Lines". */
const PREVIEW_LINES = 3;

interface LogSectionProps {
  title: string;
  logs: HookExecutionLog[];
  fileName: string;
}

function LogSection({ title, logs, fileName }: LogSectionProps) {
  const t = useTranslations("admin.hooks.logs");
  const unknown = t("unknownError.label");
  const text = formatHookLogs(logs, unknown);

  return (
    <CodePanel>
      <CodePanel.Header
        title={title}
        rightChildren={
          <Section flexDirection="row" width="fit" gap={0.25}>
            <Text font="secondary-body" color="text-03">
              {t("lineCount.label", { count: logs.length })}
            </Text>
            <Button
              icon={SvgCopy}
              prominence="tertiary"
              size="sm"
              tooltip={t("copy.tooltip")}
              onClick={() => void copyText(text)}
            />
            <Button
              icon={SvgDownload}
              prominence="tertiary"
              size="sm"
              tooltip={t("download.tooltip")}
              onClick={() => downloadFile(fileName, { content: text })}
            />
          </Section>
        }
      />
      <CodePanel.Lines
        lines={logs.map((log, index) => ({
          content: formatHookLogLine(log, unknown),
          number: index + 1,
        }))}
      />
    </CodePanel>
  );
}

interface HookLogsModalProps {
  hook: Hook;
  pointName: string;
  onClose: () => void;
}

export function HookLogsModal({
  hook,
  pointName,
  onClose,
}: HookLogsModalProps) {
  const t = useTranslations("admin.hooks.logs");
  const logs = useHookExecutionLogs(hook.id);
  const [now] = useState(() => Date.now());
  const grouped = groupHookLogs(logs.data ?? [], now);

  return (
    <Modal open onOpenChange={onClose}>
      <Modal.Content width="lg" height="fit" maxHeight="80dvh">
        <Modal.Header
          title={t("header.title")}
          description={t("header.description", {
            name: hook.name,
            point: pointName,
          })}
          onClose={onClose}
        />
        <Modal.Body>
          {logs.isLoading ? (
            <LineLoader />
          ) : logs.error ? (
            <MessageCard variant="error" title={t("loadFailed.message")} />
          ) : (logs.data ?? []).length === 0 ? (
            <Text as="p" color="text-03">
              {t("empty.message")}
            </Text>
          ) : (
            <Section gap={1} alignItems="stretch">
              {grouped.pastHour.length > 0 && (
                <LogSection
                  title={t("pastHour.label")}
                  logs={grouped.pastHour}
                  fileName={`hook-${hook.id}-errors-past-hour.log`}
                />
              )}
              {grouped.older.length > 0 && (
                <LogSection
                  title={t("older.label")}
                  logs={grouped.older}
                  fileName={`hook-${hook.id}-errors-older.log`}
                />
              )}
            </Section>
          )}
        </Modal.Body>
      </Modal.Content>
    </Modal>
  );
}

interface HookStatusProps {
  hook: Hook;
  onViewLogs: () => void;
}

/** The connection badge; a click opens a summary of the past hour's errors. */
export function HookStatus({ hook, onViewLogs }: HookStatusProps) {
  const t = useTranslations("admin.hooks");
  const [open, setOpen] = useState(false);
  const logs = useHookExecutionLogs(open ? hook.id : null);
  const grouped = groupHookLogs(logs.data ?? [], Date.now());
  const lost = hook.is_reachable === false;
  const count = grouped.pastHour.length;

  function viewLogs() {
    setOpen(false);
    onViewLogs();
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button prominence="tertiary" size="md">
          {lost
            ? t("status.connectionLost.label")
            : t("status.connected.label")}
        </Button>
      </Popover.Trigger>
      <Popover.Content align="end" width={80} padding={2} gap={1}>
        <Section gap={0.5} alignItems="stretch">
          <Badge
            color={lost ? "error" : "success"}
            size={5}
            title={
              lost
                ? t("status.connectionLost.label")
                : t("status.connected.label")
            }
          />
          <Content
            title={
              count > 0
                ? t("status.errorCount.title", { count })
                : t("status.noError.title")
            }
            description={t("status.pastHour.description")}
          />
          {logs.error && (
            <Text as="p" font="secondary-body" color="text-03">
              {t("logs.loadFailed.message")}
            </Text>
          )}
          {count > 0 && (
            <>
              <Text as="p" font="secondary-action" color="text-03">
                {t("status.recentErrors.title")}
              </Text>
              {grouped.pastHour.slice(0, PREVIEW_LINES).map((log, index) => (
                <Text
                  key={`${log.created_at}-${index}`}
                  as="p"
                  font="secondary-mono"
                  color="text-04"
                  maxLines={2}
                >
                  {log.error_message || t("logs.unknownError.label")}
                </Text>
              ))}
              <Button prominence="secondary" size="sm" onClick={viewLogs}>
                {t("status.viewMore.label")}
              </Button>
            </>
          )}
          {count === 0 && grouped.older.length > 0 && (
            <Button prominence="secondary" size="sm" onClick={viewLogs}>
              {t("status.viewOlder.label")}
            </Button>
          )}
        </Section>
      </Popover.Content>
    </Popover>
  );
}
