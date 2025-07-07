import time
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from ee.hogai.assistant import Assistant
from ee.hogai.utils.sse import AssistantSSESerializer
from ee.hogai.utils.types import AssistantMode
from ee.models import Conversation
from posthog.models import Team, User
from posthog.redis import get_async_client
from posthog.schema import HumanMessage
from posthog.temporal.common.base import PostHogWorkflow

CONVERSATION_STREAM_PREFIX = "conversation_updates:"
REDIS_STREAM_MAX_LENGTH = 1000  # Maximum number of messages to keep in stream
REDIS_STREAM_EXPIRATION_TIME = 30 * 60  # 30 minutes


@dataclass
class ConversationInputs:
    """Inputs for the conversation processing workflow."""

    team_id: int
    user_id: int
    conversation_id: UUID
    message: Optional[dict[str, Any]] = None
    contextual_tools: Optional[dict[str, Any]] = None
    is_new_conversation: bool = False
    trace_id: Optional[str] = None
    mode: AssistantMode = AssistantMode.ASSISTANT


@workflow.defn(name="conversation-processing")
class ConversationWorkflow(PostHogWorkflow):
    """Temporal workflow for processing AI conversations asynchronously."""

    @staticmethod
    def parse_inputs(inputs: list[str]) -> ConversationInputs:
        """Parse inputs from the management command CLI."""
        loaded = json.loads(inputs[0])
        return ConversationInputs(**loaded)

    @workflow.run
    async def run(self, inputs: ConversationInputs) -> None:
        """Execute the conversation processing workflow."""

        # Process the conversation with retries
        await workflow.execute_activity(
            process_conversation_activity,
            inputs,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        )


@activity.defn
async def process_conversation_activity(inputs: ConversationInputs) -> None:
    """Asynchronous conversation processing function that streams chunks immediately."""

    try:
        team = await Team.objects.aget(id=inputs.team_id)
        user = await User.objects.aget(id=inputs.user_id)
        conversation = await Conversation.objects.aget(id=inputs.conversation_id)
    except Team.DoesNotExist:
        raise ValueError(f"Team {inputs.team_id} not found")
    except User.DoesNotExist:
        raise ValueError(f"User {inputs.user_id} not found")
    except Conversation.DoesNotExist:
        raise ValueError(f"Conversation {inputs.conversation_id} not found")

    human_message = HumanMessage.model_validate(inputs.message) if inputs.message else None

    assistant = Assistant(
        team,
        conversation,
        new_message=human_message,
        user=user,
        contextual_tools=inputs.contextual_tools,
        is_new_conversation=inputs.is_new_conversation,
        trace_id=inputs.trace_id,
        mode=inputs.mode,
    )

    stream_key = get_conversation_stream_key(inputs.conversation_id)
    redis_client = get_async_client()

    def get_timestamp_ms() -> str:
        """Get current timestamp in milliseconds as string."""
        return str(int(time.time() * 1000))

    try:
        chunk_count = 0

        serializer = AssistantSSESerializer()
        async for chunk in assistant.astream():
            chunk_count += 1

            # Add chunk to Redis stream immediately
            await redis_client.xadd(
                stream_key,
                {"data": serializer.dumps(chunk), "timestamp": get_timestamp_ms()},
                maxlen=REDIS_STREAM_MAX_LENGTH,
                approximate=True,
            )

        # Mark the stream as complete
        await redis_client.xadd(
            stream_key,
            {"status": "complete", "timestamp": get_timestamp_ms()},
            maxlen=REDIS_STREAM_MAX_LENGTH,
            approximate=True,
        )

        # Set stream expiration (30 minutes)
        await redis_client.expire(stream_key, REDIS_STREAM_EXPIRATION_TIME)

    except Exception as e:
        # Mark the stream as failed
        await redis_client.xadd(
            stream_key,
            {
                "status": "error",
                "error": str(e),
                "timestamp": get_timestamp_ms(),
            },
            maxlen=REDIS_STREAM_MAX_LENGTH,
            approximate=True,
        )

        raise
    finally:
        await redis_client.close()


def get_conversation_stream_key(conversation_id: UUID) -> str:
    """Get the Redis stream key for a conversation."""
    return f"{CONVERSATION_STREAM_PREFIX}{conversation_id}"
