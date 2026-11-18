"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  BasicModalFooter,
  Button,
  Card,
  LinkButton,
  Modal,
} from "@opal/components";
import { ContentAction, toast } from "@opal/layouts";
import { SvgDownload } from "@opal/icons";
import { SvgClaude, SvgOpenAI } from "@opal/logos";
import { DOCS_BASE_URL } from "@/lib/constants";
import { downloadOnyxPlugin } from "@/lib/integrations/svc";

const MCP_SERVER_DOCS_URL = `${DOCS_BASE_URL}/overview/onyx_anywhere/mcp_server`;

const CLIENTS = [
  {
    key: "claude",
    icon: SvgClaude,
    guideUrl: `${MCP_SERVER_DOCS_URL}/claude#upload-the-onyx-plugin`,
  },
  {
    key: "chatgpt",
    icon: SvgOpenAI,
    guideUrl: `${MCP_SERVER_DOCS_URL}/chatgpt#chatgpt-on-the-web`,
  },
] as const;

interface ExportOnyxPluginModalProps {
  onClose: () => void;
}

export default function ExportOnyxPluginModal({
  onClose,
}: ExportOnyxPluginModalProps) {
  const t = useTranslations("admin.plugins.export");
  const [downloading, setDownloading] = useState(false);

  async function download() {
    setDownloading(true);
    try {
      await downloadOnyxPlugin();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("downloadFailed"));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <Modal open onOpenChange={(open) => !open && onClose()}>
      <Modal.Content width="md">
        <Modal.Header
          icon={SvgDownload}
          title={t("title")}
          description={t("description")}
          onClose={onClose}
        />
        <Modal.Body padding={4} gap={2} alignItems="stretch">
          {CLIENTS.map(({ key, icon, guideUrl }) => (
            <Card key={key} border="solid" padding={2}>
              <ContentAction
                font="main-ui-action"
                icon={icon}
                title={t(`${key}.title`)}
                description={t(`${key}.description`)}
                center
                rightChildren={
                  <LinkButton href={guideUrl} target="_blank">
                    {t(`${key}.guide`)}
                  </LinkButton>
                }
              />
            </Card>
          ))}
        </Modal.Body>
        <Modal.Footer>
          <BasicModalFooter
            submit={
              <Button
                icon={SvgDownload}
                disabled={downloading}
                onClick={() => void download()}
              >
                {t("download")}
              </Button>
            }
          />
        </Modal.Footer>
      </Modal.Content>
    </Modal>
  );
}
