import structlog
from typing import Any
from collections.abc import AsyncGenerator
from uuid import uuid4

from django.conf import settings

from ee.hogai.stream.redis_stream import RedisStream, RedisStreamError
from ee.models.assistant import Conversation
from ee.hogai.utils.types import AssistantMode
from posthog.constants import MAX_AI_TASK_QUEUE
from posthog.schema import FailureMessage
from posthog.temporal.common.client import connect
from posthog.temporal.ai.conversation import ConversationInputs, ConversationWorkflow, get_conversation_stream_key

logger = structlog.get_logger(__name__)


# Redis stream configuration
REDIS_STREAM_MAX_LENGTH = 1000  # Maximum number of messages to keep in stream
STREAM_CREATION_WAIT_ATTEMPTS = 10  # Maximum attempts to wait for stream creation (renamed from REDIS_EXPIRATION_TIME)
STREAM_TIMEOUT_SECONDS = 300  # 5 minutes max streaming time


class ConversationStreamManager:
    """Manages conversation streaming from Redis streams."""

    def __init__(self, conversation: Conversation, team_id: int) -> None:
        self.conversation = conversation
        self.team_id = team_id
        self.redis_stream = RedisStream(conversation.id, get_conversation_stream_key(conversation.id), team_id)

    async def start_workflow_and_stream(
        self, user_id: int, validated_data: dict[str, Any], is_new_conversation: bool
    ) -> AsyncGenerator[str, Any]:
        """Process a new message and stream the response."""
        try:
            # Start Temporal workflow using async client
            client = await connect(
                settings.TEMPORAL_HOST,
                settings.TEMPORAL_PORT,
                settings.TEMPORAL_NAMESPACE,
                settings.TEMPORAL_CLIENT_ROOT_CA,
                settings.TEMPORAL_CLIENT_CERT,
                settings.TEMPORAL_CLIENT_KEY,
            )

            workflow_inputs = ConversationInputs(
                team_id=self.team_id,
                user_id=user_id,
                conversation_id=self.conversation.id,
                message=validated_data["message"].model_dump() if validated_data["message"] else None,
                contextual_tools=validated_data.get("contextual_tools"),
                is_new_conversation=is_new_conversation,
                trace_id=str(validated_data["trace_id"]),
                mode=AssistantMode.ASSISTANT,
            )

            await client.start_workflow(
                ConversationWorkflow.run,
                workflow_inputs,
                id=f"conversation-{self.conversation.id}-{uuid4()}",
                task_queue=MAX_AI_TASK_QUEUE,
            )

            async for chunk in self.stream_conversation():
                yield chunk

        except Exception as e:
            logger.exception("Failed to start async conversation processing", error=e)
            # Clean up any potentially created resources
            try:
                await self.redis_stream.delete_stream("workflow start failure cleanup")
            except Exception:
                pass  # Best effort cleanup
            yield self._yield_failure_message()

    async def stream_conversation(self) -> AsyncGenerator[str, Any]:
        """Stream conversation updates from Redis stream."""
        try:
            # Wait for stream to be created
            is_stream_available = await self.redis_stream.wait_for_stream_creation()
            if not is_stream_available:
                yield self._yield_failure_message()
                return

            # Process historical messages first
            message_chunks, historical_messages, conversation_completed = await self._process_historical_messages()

            for message_chunk in message_chunks:
                if message_chunk:  # Only yield non-empty messages
                    yield message_chunk

            if conversation_completed:
                return

            # Stream live messages
            start_id = self._determine_live_stream_start_id(historical_messages)
            try:
                async for chunk in self.redis_stream.read_stream(start_id=start_id):
                    yield chunk
            except RedisStreamError as e:
                logger.exception("Error streaming live messages", error=e)
                yield self._yield_failure_message()

            # If we reach here, the stream has completed - delete it
            await self.redis_stream.delete_stream("stream completed")

        except Exception as e:
            logger.exception("Error streaming conversation", error=e)
            # Clean up the stream on error
            await self.redis_stream.delete_stream("error cleanup")
            yield self._yield_failure_message()

    def _determine_live_stream_start_id(
        self, historical_messages: list[tuple[str, dict[bytes, bytes]]] | None = None
    ) -> str:
        """Determine the correct start ID for live streaming based on historical messages.

        Args:
            historical_messages: List of (message_id, fields) tuples from Redis stream

        Returns:
            Redis stream ID to start reading from ('0', '$', or specific ID)
        """
        if not historical_messages:
            return "0"

        # Find the highest message_id from historical messages to ensure we don't miss any
        # Redis stream IDs are in format "timestamp-sequence", so we can sort them
        sorted_messages = sorted(historical_messages, key=lambda x: x[0])
        last_id = sorted_messages[-1][0]

        # Use "$" for new messages only if we've processed all historical messages
        return "$" if not historical_messages else last_id

    async def _process_historical_messages(self) -> tuple[list[str], list[tuple[str, dict[bytes, bytes]]], bool]:
        """Process historical messages from Redis stream.

        Returns:
            tuple: (message_chunks, historical_messages, conversation_completed)
                - message_chunks: List of decoded message data strings
                - historical_messages: Raw Redis stream messages
                - conversation_completed: Whether the conversation has finished
        """
        historical_messages = await self.redis_stream.get_stream_history(count=REDIS_STREAM_MAX_LENGTH)
        message_chunks = []
        conversation_completed = False

        for _, fields in historical_messages:
            if b"data" in fields:
                decoded_data = self.redis_stream.safe_decode(fields[b"data"], "data")
                if decoded_data:
                    message_chunks.append(decoded_data)
            elif b"status" in fields:
                status = self.redis_stream.safe_decode(fields[b"status"], "status")
                if status == "complete":
                    await self.redis_stream.delete_stream("conversation completed")
                    conversation_completed = True
                    break
                elif status == "error":
                    error = self.redis_stream.safe_decode(fields.get(b"error", b"Unknown error"), "error")
                    if error:
                        message_chunks.append(self._yield_failure_message())
                    await self.redis_stream.delete_stream("conversation failed")
                    conversation_completed = True
                    break

        return message_chunks, historical_messages, conversation_completed

    def _yield_failure_message(self) -> str:
        """Format a failure message as SSE chunk.

        Returns:
            Server-sent event formatted string with error message
        """
        failure_message = FailureMessage(
            content="Oops! Something went wrong. Please try again.",
            id=str(uuid4()),
        )
        return f"event: message\ndata: {failure_message.model_dump_json(exclude_none=True)}\n\n"
