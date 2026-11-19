"use client";

import { useFormatter, useTranslations } from "next-intl";
import { Button, Card, MessageCard, Text } from "@opal/components";
import { SvgExternalLink, SvgFileText } from "@opal/icons";
import { Content, Section, SettingsLayouts } from "@opal/layouts";
import { useParams } from "@opal/navigation";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { useChatSessionSnapshot } from "@/lib/queryHistory/hooks";
import type {
  AcceptedInputSnapshot,
  MessageSnapshot,
} from "@/lib/queryHistory/types";
import FeedbackBadge from "@/views/admin/QueryHistoryPage/FeedbackBadge";

const route = ADMIN_ROUTES.QUERY_HISTORY;

interface AcceptedInputProps {
  input: AcceptedInputSnapshot;
}

function AcceptedInput({ input }: AcceptedInputProps) {
  const t = useTranslations("admin.queryHistory.detail");
  return (
    <Section gap={0.25} alignItems="start">
      <Text font="secondary-action" color="text-03" as="p">
        {input.source === "human" ? t("user.label") : t("agent.label")}
      </Text>
      <Text font="main-content-body" as="p">
        {input.content}
      </Text>
    </Section>
  );
}

interface MessageCardViewProps {
  message: MessageSnapshot;
  userEmail: string | null;
}

function MessageView({ message, userEmail }: MessageCardViewProps) {
  const t = useTranslations("admin.queryHistory.detail");
  const isUser = message.message_type === "user";
  return (
    <Card border="solid" rounding={4} padding={1}>
      <Section gap={0.75} alignItems="stretch">
        <Text font="main-ui-action" as="p">
          {isUser ? (userEmail ?? t("user.label")) : t("ai.label")}
        </Text>
        <Text font="main-content-body" as="p">
          {message.message}
        </Text>
        {message.accepted_inputs.map((input, index) => (
          <AcceptedInput key={index} input={input} />
        ))}
        {!isUser && message.documents.length > 0 && (
          <Section gap={0.25} alignItems="start">
            <Text font="secondary-action" color="text-03" as="p">
              {t("referenceDocuments.title")}
            </Text>
            {message.documents.map((doc) =>
              doc.link ? (
                <Button
                  key={doc.document_id}
                  prominence="tertiary"
                  icon={SvgExternalLink}
                  href={doc.link}
                  target="_blank"
                >
                  {doc.semantic_identifier}
                </Button>
              ) : (
                <Content
                  key={doc.document_id}
                  icon={SvgFileText}
                  font="secondary-body"
                  title={doc.semantic_identifier}
                />
              )
            )}
          </Section>
        )}
        {!isUser && message.feedback_type && (
          <Section gap={0.25} alignItems="start">
            <Text font="secondary-action" color="text-03" as="p">
              {t("feedback.title")}
            </Text>
            <FeedbackBadge feedbackType={message.feedback_type} />
            {message.feedback_text && (
              <Text font="main-ui-body" as="p">
                {message.feedback_text}
              </Text>
            )}
          </Section>
        )}
      </Section>
    </Card>
  );
}

export default function ChatSessionDetailPage() {
  const t = useTranslations("admin.queryHistory");
  const format = useFormatter();
  const params = useParams<{ id: string }>();
  const session = useChatSessionSnapshot(params?.id);

  const meta = session.data
    ? [
        session.data.user_email,
        session.data.persona_name ?? t("assistant.unknown.label"),
        format.dateTime(new Date(session.data.time_created), {
          dateStyle: "medium",
          timeStyle: "short",
        }),
      ]
        .filter((part): part is string => Boolean(part))
        .join(" · ")
    : undefined;

  return (
    <SettingsLayouts.Root width="lg" loading={session.isLoading}>
      <SettingsLayouts.Header
        icon={route.icon}
        title={t("detail.header.title")}
        description={meta}
        cancel={route.path}
        divider
      />
      <SettingsLayouts.Body>
        {session.error ? (
          <MessageCard
            variant="error"
            title={t("detail.errorTitle.title")}
            description={t("detail.fetchFailed.message", {
              error:
                session.error instanceof Error
                  ? session.error.message
                  : String(session.error),
            })}
          />
        ) : (
          <Section gap={0.75} alignItems="stretch">
            {(session.data?.messages ?? []).map((message) => (
              <MessageView
                key={message.id}
                message={message}
                userEmail={session.data?.user_email ?? null}
              />
            ))}
          </Section>
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
