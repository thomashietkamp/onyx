"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Card, LinkButton, Modal } from "@opal/components";
import { ContentAction, Section, toast } from "@opal/layouts";
import { SvgDownload } from "@opal/icons";
import { SvgClaude, SvgOpenAI } from "@opal/logos";
import { DOCS_BASE_URL } from "@/lib/constants";
import { downloadOnyxPlugin } from "@/lib/integrations/svc";

const MCP_SERVER_DOCS_URL = `${DOCS_BASE_URL}/overview/onyx_anywhere/mcp_server`;

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
          <Card border="solid" padding={2}>
            <ContentAction
              font="main-ui-action"
              icon={SvgClaude}
              title={t("claude.title")}
              description={t("claude.description")}
              center
              rightChildren={
                <Section flexDirection="row" gap={3} width="fit">
                  <LinkButton
                    href={`${MCP_SERVER_DOCS_URL}/claude#upload-the-onyx-plugin`}
                    target="_blank"
                  >
                    {t("claude.guide")}
                  </LinkButton>
                  <Button
                    icon={SvgDownload}
                    disabled={downloading}
                    onClick={() => void download()}
                  >
                    {t("claude.download")}
                  </Button>
                </Section>
              }
            />
          </Card>
          <Card border="solid" padding={2}>
            <ContentAction
              font="main-ui-action"
              icon={SvgOpenAI}
              title={t("chatgpt.title")}
              description={t("chatgpt.description")}
              center
              rightChildren={
                <LinkButton
                  href={`${MCP_SERVER_DOCS_URL}/chatgpt#chatgpt-on-the-web`}
                  target="_blank"
                >
                  {t("chatgpt.guide")}
                </LinkButton>
              }
            />
          </Card>
        </Modal.Body>
      </Modal.Content>
    </Modal>
  );
}
