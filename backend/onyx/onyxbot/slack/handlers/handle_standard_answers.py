from slack_sdk import WebClient
from slack_sdk.models.blocks import Block, HeaderBlock, MarkdownTextObject, SectionBlock
from sqlalchemy.orm import Session

from onyx.db.models import SlackChannelConfig
from onyx.db.standard_answer import find_matching_standard_answers
from onyx.onyxbot.slack.models import SlackMessageInfo
from onyx.onyxbot.slack.utils import respond_in_thread_or_channel
from onyx.utils.logger import OnyxLoggingAdapter, setup_logger
from onyx.utils.variable_functionality import fetch_versioned_implementation

logger = setup_logger()


def handle_standard_answers(
    message_info: SlackMessageInfo,
    receiver_ids: list[str] | None,
    slack_channel_config: SlackChannelConfig,
    logger: OnyxLoggingAdapter,
    client: WebClient,
    db_session: Session,
) -> bool:
    """Returns whether one or more Standard Answer message blocks were
    emitted by the Slack bot"""
    versioned_handle_standard_answers = fetch_versioned_implementation(
        "onyx.onyxbot.slack.handlers.handle_standard_answers",
        "_handle_standard_answers",
    )
    return versioned_handle_standard_answers(
        message_info=message_info,
        receiver_ids=receiver_ids,
        slack_channel_config=slack_channel_config,
        logger=logger,
        client=client,
        db_session=db_session,
    )


def _handle_standard_answers(
    message_info: SlackMessageInfo,
    receiver_ids: list[str] | None,
    slack_channel_config: SlackChannelConfig,
    logger: OnyxLoggingAdapter,
    client: WebClient,
    db_session: Session,
) -> bool:
    """Posts the standard answers that match the question, in its thread.
    Returns True when it posted, so the bot skips the regular answer."""
    category_ids = [
        category.id for category in slack_channel_config.standard_answer_categories
    ]
    if not category_ids or not message_info.thread_messages:
        return False
    query = message_info.thread_messages[-1].message
    answers = find_matching_standard_answers(db_session, category_ids, query)
    if not answers:
        return False

    blocks: list[Block] = [
        HeaderBlock(text="Standard answer" if len(answers) == 1 else "Standard answers")
    ]
    blocks.extend(
        SectionBlock(text=MarkdownTextObject(text=answer.answer)) for answer in answers
    )
    try:
        respond_in_thread_or_channel(
            client=client,
            channel=message_info.channel_to_respond,
            thread_ts=message_info.msg_to_respond,
            text="\n\n".join(answer.answer for answer in answers),
            blocks=blocks,
            receiver_ids=receiver_ids,
        )
    except Exception:
        logger.exception("Could not post standard answers")
        return False
    return True
