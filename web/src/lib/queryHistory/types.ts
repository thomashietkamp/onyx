export type QAFeedbackType = "like" | "dislike" | "mixed";

export type SessionFlowType = "Chat" | "Search" | "Slack";

export interface ChatSessionMinimal {
  id: string;
  /** Null when the workspace anonymizes query history. */
  user_email: string | null;
  name: string | null;
  first_user_message: string;
  first_ai_message: string;
  persona_name: string | null;
  time_created: string;
  feedback_type: QAFeedbackType | null;
  flow_type: SessionFlowType;
}

export interface ChatSessionHistoryPage {
  items: ChatSessionMinimal[];
  total_items: number;
}

export interface AbridgedSearchDoc {
  document_id: string;
  semantic_identifier: string;
  link: string | null;
}

export type AcceptedInputSource = "human" | "agent" | "agent_result";

export interface AcceptedInputSnapshot {
  source: AcceptedInputSource;
  content: string;
}

export interface MessageSnapshot {
  id: number;
  message: string;
  message_type: "user" | "assistant";
  documents: AbridgedSearchDoc[];
  feedback_type: QAFeedbackType | null;
  feedback_text: string | null;
  accepted_inputs: AcceptedInputSnapshot[];
  time_created: string;
}

export interface ChatSessionSnapshot {
  id: string;
  user_email: string | null;
  name: string | null;
  messages: MessageSnapshot[];
  persona_name: string | null;
  time_created: string;
  feedback_type: QAFeedbackType | null;
  flow_type: SessionFlowType;
}

export type QueryHistoryExportStatus = "PENDING" | "SUCCESS" | "FAILURE";

export interface QueryHistoryExport {
  request_id: string;
  status: QueryHistoryExportStatus;
  start: string | null;
  end: string | null;
  time_requested: string;
}

export interface QueryHistoryFilters {
  feedbackType: QAFeedbackType | null;
  start: Date | null;
  end: Date | null;
  pageNum: number;
  pageSize: number;
}
