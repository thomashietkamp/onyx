"""Turns chat sessions into query history views, and names export jobs.

The API and the export task share these, so the table, the detail page and
the CSV show the same data and hide user emails in the same way.
"""

from collections.abc import Sequence
from datetime import datetime

from onyx.configs.constants import (
    QUERY_HISTORY_TASK_NAME_PREFIX,
    MessageType,
    QAFeedbackType,
    QueryHistoryType,
    SessionType,
)
from onyx.db.models import ChatMessage, ChatSession
from onyx.db.query_history import (
    SessionWithFeedback,
    feedback_type_from_flags,
    mainline_messages,
)
from onyx.server.query_history.models import (
    AbridgedSearchDoc,
    AcceptedInputSnapshot,
    ChatSessionMinimal,
    ChatSessionSnapshot,
    MessageSnapshot,
)
from onyx.utils.csv_utils import sanitize_csv_cell

_SHOWN_MESSAGE_TYPES = (MessageType.USER, MessageType.ASSISTANT)

CSV_HEADER = [
    "chat_session_id",
    "user_email",
    "persona_name",
    "flow_type",
    "question_index",
    "user_message",
    "ai_response",
    "feedback_type",
    "feedback_text",
    "reference_documents",
    "time_created",
]


def anonymizes(query_history_type: QueryHistoryType | None) -> bool:
    return query_history_type == QueryHistoryType.ANONYMIZED


def export_task_name(request_id: str, start: datetime, end: datetime) -> str:
    """The task row name. It holds the range, so the exports list can show
    the range of a job that has no file yet."""
    return (
        f"{QUERY_HISTORY_TASK_NAME_PREFIX}_{request_id}"
        f"_{start.isoformat()}_{end.isoformat()}"
    )


def export_range_from_task_name(
    task_name: str,
) -> tuple[datetime, datetime] | None:
    parts = task_name.removeprefix(f"{QUERY_HISTORY_TASK_NAME_PREFIX}_").split("_")
    if len(parts) != 3:
        return None
    try:
        return datetime.fromisoformat(parts[1]), datetime.fromisoformat(parts[2])
    except ValueError:
        return None


def _user_email(chat_session: ChatSession, anonymize: bool) -> str | None:
    if anonymize or chat_session.user is None:
        return None
    return chat_session.user.email


def _flow_type(chat_session: ChatSession) -> SessionType:
    return SessionType.SLACK if chat_session.onyxbot_flow else SessionType.CHAT


def _persona_name(chat_session: ChatSession) -> str | None:
    return chat_session.persona.name if chat_session.persona is not None else None


def to_minimal(
    row: SessionWithFeedback,
    first_messages: dict[MessageType, str],
    anonymize: bool,
) -> ChatSessionMinimal:
    chat_session, has_like, has_dislike = row
    return ChatSessionMinimal(
        id=chat_session.id,
        user_email=_user_email(chat_session, anonymize),
        name=chat_session.description,
        first_user_message=first_messages.get(MessageType.USER, ""),
        first_ai_message=first_messages.get(MessageType.ASSISTANT, ""),
        persona_name=_persona_name(chat_session),
        time_created=chat_session.time_created,
        feedback_type=feedback_type_from_flags(has_like, has_dislike),
        flow_type=_flow_type(chat_session),
    )


def _message_snapshot(message: ChatMessage) -> MessageSnapshot:
    latest_feedback = max(
        message.chat_message_feedbacks, key=lambda feedback: feedback.id, default=None
    )
    feedback_type: QAFeedbackType | None = None
    if latest_feedback is not None and latest_feedback.is_positive is not None:
        feedback_type = (
            QAFeedbackType.LIKE
            if latest_feedback.is_positive
            else QAFeedbackType.DISLIKE
        )
    return MessageSnapshot(
        id=message.id,
        message=message.message,
        message_type=message.message_type,
        documents=[
            AbridgedSearchDoc(
                document_id=doc.document_id,
                semantic_identifier=doc.semantic_id,
                link=doc.link,
            )
            for doc in message.search_docs
        ],
        feedback_type=feedback_type,
        feedback_text=(
            latest_feedback.feedback_text if latest_feedback is not None else None
        ),
        accepted_inputs=[
            AcceptedInputSnapshot(source=accepted.source, content=accepted.content)
            for item in sorted(message.response_items, key=lambda i: i.step_index)
            for accepted in item.inputs
        ],
        time_created=message.time_sent,
    )


def to_snapshot(
    row: SessionWithFeedback,
    messages: Sequence[ChatMessage],
    anonymize: bool,
) -> ChatSessionSnapshot:
    chat_session, has_like, has_dislike = row
    return ChatSessionSnapshot(
        id=chat_session.id,
        user_email=_user_email(chat_session, anonymize),
        name=chat_session.description,
        messages=[
            _message_snapshot(message)
            for message in mainline_messages(messages)
            if message.message_type in _SHOWN_MESSAGE_TYPES
        ],
        persona_name=_persona_name(chat_session),
        time_created=chat_session.time_created,
        feedback_type=feedback_type_from_flags(has_like, has_dislike),
        flow_type=_flow_type(chat_session),
    )


def _csv_row(
    snapshot: ChatSessionSnapshot,
    index: int,
    question: MessageSnapshot | None,
    answer: MessageSnapshot | None,
) -> list[str]:
    user_text = question.message if question is not None else ""
    if answer is not None and answer.accepted_inputs:
        accepted = "\n".join(i.content for i in answer.accepted_inputs)
        user_text = f"{user_text}\n{accepted}" if user_text else accepted
    first = question or answer
    return [
        str(snapshot.id),
        snapshot.user_email or "",
        snapshot.persona_name or "",
        snapshot.flow_type.value,
        str(index),
        user_text,
        answer.message if answer is not None else "",
        answer.feedback_type.value if answer and answer.feedback_type else "",
        (answer.feedback_text or "") if answer is not None else "",
        "\n".join(doc.link or doc.semantic_identifier for doc in answer.documents)
        if answer is not None
        else "",
        first.time_created.isoformat() if first is not None else "",
    ]


def csv_rows(snapshot: ChatSessionSnapshot) -> list[list[str]]:
    """One row per question and its answer. A question with no answer gets a
    row with an empty answer. Cells are sanitized against spreadsheet formula
    injection, since users write the messages."""
    rows: list[list[str]] = []
    question: MessageSnapshot | None = None
    for message in snapshot.messages:
        if message.message_type == MessageType.USER:
            if question is not None:
                rows.append(_csv_row(snapshot, len(rows), question, None))
            question = message
            continue
        rows.append(_csv_row(snapshot, len(rows), question, message))
        question = None
    if question is not None:
        rows.append(_csv_row(snapshot, len(rows), question, None))
    return [[sanitize_csv_cell(cell) for cell in row] for row in rows]
