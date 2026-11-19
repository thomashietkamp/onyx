from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from onyx.agents.models import InputSource
from onyx.configs.constants import MessageType, QAFeedbackType, SessionType


class AbridgedSearchDoc(BaseModel):
    """A document an answer used, as the detail page lists it."""

    document_id: str
    semantic_identifier: str
    link: str | None


class AcceptedInputSnapshot(BaseModel):
    """A message that an answer accepted while it ran, such as a user message
    sent mid-response or a background agent result."""

    source: InputSource
    content: str


class MessageSnapshot(BaseModel):
    id: int
    message: str
    message_type: MessageType
    documents: list[AbridgedSearchDoc]
    feedback_type: QAFeedbackType | None
    feedback_text: str | None
    # Answers only, in model order.
    accepted_inputs: list[AcceptedInputSnapshot]
    time_created: datetime


class ChatSessionMinimal(BaseModel):
    """One row of the query history table."""

    id: UUID
    # None when the workspace anonymizes query history.
    user_email: str | None
    name: str | None
    first_user_message: str
    first_ai_message: str
    persona_name: str | None
    time_created: datetime
    feedback_type: QAFeedbackType | None
    flow_type: SessionType


class ChatSessionSnapshot(BaseModel):
    """One session with the messages of its selected branch."""

    id: UUID
    user_email: str | None
    name: str | None
    messages: list[MessageSnapshot]
    persona_name: str | None
    time_created: datetime
    feedback_type: QAFeedbackType | None
    flow_type: SessionType


class ChatSessionsForUser(BaseModel):
    sessions: list[ChatSessionMinimal]


class QueryHistoryExportStatus(str, Enum):
    """The state of an export as the admin sees it. A started export is
    pending until its file is published."""

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class QueryHistoryExport(BaseModel):
    request_id: str
    status: QueryHistoryExportStatus
    start: datetime | None
    end: datetime | None
    # When the export was requested.
    time_requested: datetime


class StartQueryHistoryExportResponse(BaseModel):
    request_id: str


class QueryHistoryExportStatusResponse(BaseModel):
    status: QueryHistoryExportStatus
