"use client";

import { useTranslations, useFormatter } from "next-intl";
import {
  Badge,
  Button,
  MessageCard,
  Modal,
  Table,
  Text,
  type TableColumn,
} from "@opal/components";
import { SvgDownload, SvgHistory } from "@opal/icons";
import { toast } from "@opal/layouts";
import { useQueryHistoryExports } from "@/lib/queryHistory/hooks";
import { downloadQueryHistoryExport } from "@/lib/queryHistory/svc";
import type {
  QueryHistoryExport,
  QueryHistoryExportStatus,
} from "@/lib/queryHistory/types";

interface ExportStatusBadgeProps {
  status: QueryHistoryExportStatus;
}

function ExportStatusBadge({ status }: ExportStatusBadgeProps) {
  const t = useTranslations("admin.queryHistory.exportStatus");
  switch (status) {
    case "SUCCESS":
      return <Badge title={t("success.label")} color="success" size={5} />;
    case "FAILURE":
      return <Badge title={t("failure.label")} color="error" size={5} />;
    default:
      return <Badge title={t("pending.label")} color="pending" size={5} />;
  }
}

interface ExportsModalProps {
  onClose: () => void;
}

export default function ExportsModal({ onClose }: ExportsModalProps) {
  const t = useTranslations("admin.queryHistory");
  const format = useFormatter();
  const exports = useQueryHistoryExports(true);

  function formatTime(value: string | null): string {
    if (!value) return "-";
    return format.dateTime(new Date(value), {
      dateStyle: "medium",
      timeStyle: "short",
    });
  }

  async function download(requestId: string) {
    try {
      await downloadQueryHistoryExport(requestId);
    } catch {
      toast.error(t("export.downloadFailed.message"));
    }
  }

  const columns: TableColumn<QueryHistoryExport>[] = [
    {
      kind: "data",
      field: "time_requested",
      title: t("exportsModal.generatedAt.header"),
      weight: 25,
      cell: (value) => <Text font="main-ui-body">{formatTime(value)}</Text>,
    },
    {
      kind: "data",
      field: "start",
      title: t("exportsModal.startRange.header"),
      weight: 20,
      cell: (value) => <Text font="main-ui-body">{formatTime(value)}</Text>,
    },
    {
      kind: "data",
      field: "end",
      title: t("exportsModal.endRange.header"),
      weight: 20,
      cell: (value) => <Text font="main-ui-body">{formatTime(value)}</Text>,
    },
    {
      kind: "data",
      field: "status",
      title: t("exportsModal.status.header"),
      weight: 15,
      cell: (value) => <ExportStatusBadge status={value} />,
    },
    {
      kind: "display",
      id: "download",
      title: t("exportsModal.download.header"),
      width: { weight: 10, minWidth: 80 },
      cell: (row) => (
        <Button
          icon={SvgDownload}
          prominence="tertiary"
          disabled={row.status !== "SUCCESS"}
          tooltip={
            row.status === "SUCCESS"
              ? t("exportsModal.download.header")
              : t("exportsModal.notReady.tooltip")
          }
          onClick={() => void download(row.request_id)}
        />
      ),
    },
  ];

  return (
    <Modal open onOpenChange={onClose}>
      <Modal.Content width="lg" height="fit">
        <Modal.Header
          icon={SvgHistory}
          title={t("exportsModal.header.title")}
          onClose={onClose}
        />
        <Modal.Body>
          {exports.error ? (
            <MessageCard variant="error" title={t("table.fetchError.title")} />
          ) : (
            <Table
              label={t("exportsModal.header.title")}
              items={exports.data ?? []}
              columns={columns}
              getRowId={(row) => row.request_id}
              pageSize={10}
            />
          )}
        </Modal.Body>
        <Modal.Footer>
          <Button prominence="secondary" onClick={onClose}>
            {t("export.cancel.label")}
          </Button>
        </Modal.Footer>
      </Modal.Content>
    </Modal>
  );
}
