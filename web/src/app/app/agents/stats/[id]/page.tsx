"use client";

import { use, useEffect } from "react";
import { useNavigation } from "@opal/navigation";
import { useAgent } from "@/lib/agents/hooks";
import AgentStatsPage from "@/views/AgentStatsPage";
import { PageLoader } from "@opal/loaders";

export interface PageProps {
  params: Promise<{ id: string }>;
}

export default function Page(props: PageProps) {
  const navigation = useNavigation();
  const { id } = use(props.params);
  const agentId = parseInt(id);
  const { agent, isLoading } = useAgent(isNaN(agentId) ? null : agentId);

  useEffect(() => {
    if (isNaN(agentId) || (!isLoading && !agent)) {
      navigation.go("/app");
    }
  }, [agentId, isLoading, agent, navigation]);

  if (isLoading || !agent) return <PageLoader />;

  return <AgentStatsPage agentId={agent.id} agentName={agent.name} />;
}
