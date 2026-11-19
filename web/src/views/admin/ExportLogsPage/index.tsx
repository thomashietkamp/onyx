"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Card, MessageCard, Text } from "@opal/components";
import { ContentAction, Section, SettingsLayouts, toast } from "@opal/layouts";
import { SvgDownload, SvgFileText } from "@opal/icons";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { useAdminRouteTitle } from "@/lib/adminNavLabels";
import { FetchError } from "@/lib/fetcher";
import { useLogExportStatus } from "@/lib/logExport/hooks";
import { downloadLogExport, startLogExport } from "@/lib/logExport/svc";
import { logExportPieces } from "@/lib/logExport/utils";

const route = ADMIN_ROUTES.EXPORT_LOGS;

// Keeps the running export across a reload, so the page re-attaches to it.
const ACTIVE_EXPORT_STORAGE_KEY = "onyx.admin.logExport.activeExportId";
// The export is gone or no longer readable by this admin.
const LOST_ACCESS_STATUSES = new Set([401, 403, 404]);

type Phase = "idle" | "starting" | "collecting" | "downloading" | "done";

function readStoredExportId(): string | null {
  try {
    return window.localStorage.getItem(ACTIVE_EXPORT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeExportId(exportId: string | null): void {
  try {
    if (exportId) {
      window.localStorage.setItem(ACTIVE_EXPORT_STORAGE_KEY, exportId);
    } else {
      window.localStorage.removeItem(ACTIVE_EXPORT_STORAGE_KEY);
    }
  } catch {
    // Storage is off: the page still works, it only cannot re-attach.
  }
}

export default function ExportLogsPage() {
  const t = useTranslations("admin.exportLogs");
  const adminRouteTitle = useAdminRouteTitle();
  const [phase, setPhase] = useState<Phase>("idle");
  const [exportId, setExportId] = useState<string | null>(null);
  const downloadedExportRef = useRef<string | null>(null);
  const { status, error } = useLogExportStatus(exportId);

  useEffect(() => {
    const stored = readStoredExportId();
    if (stored) {
      setExportId(stored);
      setPhase("collecting");
    }
  }, []);

  const download = useCallback(
    async (id: string) => {
      setPhase("downloading");
      try {
        await downloadLogExport(id);
      } catch (downloadError) {
        console.error("Failed to download the log export:", downloadError);
        toast.error(t("toasts.downloadFailed.message"));
      } finally {
        storeExportId(null);
        setPhase("done");
      }
    },
    [t]
  );

  // The download starts once collection finishes, one time for each export.
  useEffect(() => {
    if (
      phase !== "collecting" ||
      status?.state !== "ready" ||
      downloadedExportRef.current === status.export_id
    ) {
      return;
    }
    downloadedExportRef.current = status.export_id;
    void download(status.export_id);
  }, [phase, status, download]);

  useEffect(() => {
    if (!error) return;
    console.error("Failed to read the log export status:", error);
    toast.error(
      error instanceof FetchError && LOST_ACCESS_STATUSES.has(error.status)
        ? t("toasts.lostAccess.message")
        : t("toasts.unreachable.message")
    );
    storeExportId(null);
    setExportId(null);
    setPhase("idle");
  }, [error, t]);

  async function start(): Promise<void> {
    setPhase("starting");
    try {
      const result = await startLogExport();
      if (result.kind === "alreadyRunning") {
        toast.error(t("toasts.alreadyRunning.message"));
        setPhase("idle");
        return;
      }
      storeExportId(result.exportId);
      setExportId(result.exportId);
      setPhase("collecting");
    } catch (startError) {
      console.error("Failed to start the log export:", startError);
      toast.error(t("toasts.startFailed.message"));
      setPhase("idle");
    }
  }

  const busy =
    phase === "starting" || phase === "collecting" || phase === "downloading";
  const buttonLabel =
    phase === "starting"
      ? t("button.starting.label")
      : phase === "collecting"
        ? t("button.collecting.label")
        : phase === "downloading"
          ? t("button.downloading.label")
          : t("button.export.label");
  const pieces = status ? logExportPieces(status) : [];

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={adminRouteTitle(route)}
        description={t("header.description")}
        divider
      />
      <SettingsLayouts.Body>
        <Section
          flexDirection="column"
          justifyContent="start"
          alignItems="stretch"
          gap={1}
          width="full"
          height="fit"
        >
          <MessageCard
            variant="warning"
            title={t("sensitiveWarning.title")}
            description={t("sensitiveWarning.description")}
          />
          <Card border="solid" padding={2}>
            <Section
              flexDirection="column"
              justifyContent="start"
              alignItems="stretch"
              gap={0.5}
              width="full"
              height="fit"
            >
              <ContentAction
                icon={SvgFileText}
                title={t("exportCard.title")}
                description={t("exportCard.description")}
                rightChildren={
                  <Section
                    flexDirection="row"
                    justifyContent="end"
                    alignItems="center"
                    gap={0.5}
                    width="fit"
                    height="fit"
                  >
                    {phase === "done" && status?.state === "ready" && (
                      <Button
                        prominence="secondary"
                        icon={SvgDownload}
                        onClick={() => void download(status.export_id)}
                      >
                        {t("downloadButton.label")}
                      </Button>
                    )}
                    <Button disabled={busy} onClick={() => void start()}>
                      {buttonLabel}
                    </Button>
                  </Section>
                }
              />
              {busy && pieces.length === 0 && (
                <Text font="secondary-body" color="text-03" as="p">
                  {t("status.startingCollection.label")}
                </Text>
              )}
              {pieces.map((piece) => (
                <Section
                  key={piece.workerName}
                  flexDirection="row"
                  justifyContent="between"
                  alignItems="center"
                  gap={1}
                  width="full"
                  height="fit"
                >
                  <Text font="main-ui-action">{piece.workerName}</Text>
                  <Text font="secondary-body" color="text-03">
                    {t(piece.label.key, piece.label.values)}
                  </Text>
                </Section>
              ))}
            </Section>
          </Card>
        </Section>
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
