"use client";

import { useNavigation } from "@opal/navigation";
import { IconLoader } from "@opal/loaders";
import { use, useEffect } from "react";
import { useTranslations } from "next-intl";
import { SlackChannelConfigCreationForm } from "@/app/admin/bots/[bot-id]/channels/SlackChannelConfigCreationForm";
import { ErrorCallout } from "@/components/ErrorCallout";

import { SettingsLayouts } from "@opal/layouts";
import { SvgSlack } from "@opal/logos";
import { useDocumentSets } from "@/app/admin/documents/sets/hooks";
import { useAgents } from "@/lib/agents/hooks";
import type { StandardAnswerCategoryResponse } from "@/components/standardAnswers/getStandardAnswerCategoriesIfEE";

// Standard answers are an Enterprise feature that this build does not have.
const NO_STANDARD_ANSWERS: StandardAnswerCategoryResponse = {
  paidEnterpriseFeaturesEnabled: false,
};

function NewChannelConfigContent({ slackBotId }: { slackBotId: number }) {
  const t = useTranslations("admin.slackBots");

  const {
    data: documentSets,
    isLoading: isDocSetsLoading,
    error: docSetsError,
  } = useDocumentSets();

  const {
    agents,
    isLoading: isAgentsLoading,
    error: agentsError,
  } = useAgents();

  if (isDocSetsLoading || isAgentsLoading) {
    return <IconLoader />;
  }

  if (docSetsError || !documentSets) {
    return (
      <ErrorCallout
        errorTitle={t("error.generic.title")}
        errorMsg={t("error.fetchDocumentSets.message", {
          error: docSetsError?.message ?? t("error.unknown.message"),
        })}
      />
    );
  }

  if (agentsError) {
    return (
      <ErrorCallout
        errorTitle={t("error.generic.title")}
        errorMsg={t("error.fetchAgents.message", {
          error: agentsError?.message ?? t("error.unknown.message"),
        })}
      />
    );
  }

  return (
    <SlackChannelConfigCreationForm
      slack_bot_id={slackBotId}
      documentSets={documentSets}
      personas={agents}
      standardAnswerCategoryResponse={NO_STANDARD_ANSWERS}
    />
  );
}

export default function Page(props: { params: Promise<{ "bot-id": string }> }) {
  const t = useTranslations("admin.slackBots");
  const unwrappedParams = use(props.params);
  const navigation = useNavigation();

  const slack_bot_id_raw = unwrappedParams?.["bot-id"] || null;
  const slack_bot_id = slack_bot_id_raw
    ? parseInt(slack_bot_id_raw as string, 10)
    : null;

  useEffect(() => {
    if (!slack_bot_id || isNaN(slack_bot_id)) {
      navigation.go("/admin/bots", { replace: true });
    }
  }, [slack_bot_id, navigation]);

  if (!slack_bot_id || isNaN(slack_bot_id)) {
    return null;
  }

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={SvgSlack}
        title={t("newChannel.header.title")}
        divider
        cancel={`/admin/bots/${slack_bot_id}`}
      />
      <SettingsLayouts.Body>
        <NewChannelConfigContent slackBotId={slack_bot_id} />
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
