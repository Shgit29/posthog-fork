from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ee.hogai.utils.types import AssistantMode
from ee.models import Conversation
from posthog.models import Team, User
from posthog.temporal.ai.conversation import (
    CONVERSATION_STREAM_PREFIX,
    REDIS_STREAM_EXPIRATION_TIME,
    REDIS_STREAM_MAX_LENGTH,
    ConversationInputs,
    get_conversation_stream_key,
    process_conversation_activity,
)


class TestProcessConversationActivity:
    """Test the process_conversation_activity function."""

    @pytest.fixture
    def mock_team(self):
        """Mock team object."""
        team = MagicMock(spec=Team)
        team.id = 1
        team.name = "Test Team"
        return team

    @pytest.fixture
    def mock_user(self):
        """Mock user object."""
        user = MagicMock(spec=User)
        user.id = 2
        user.email = "test@example.com"
        return user

    @pytest.fixture
    def mock_conversation(self):
        """Mock conversation object."""
        conversation = MagicMock(spec=Conversation)
        conversation.id = uuid4()
        conversation.team_id = 1
        conversation.user_id = 2
        return conversation

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client."""
        client = AsyncMock()
        client.xadd = AsyncMock()
        client.expire = AsyncMock()
        client.close = AsyncMock()
        return client

    @pytest.fixture
    def mock_assistant(self):
        """Mock Assistant with streaming capability."""
        assistant = MagicMock()

        # Mock the async stream to yield chunks
        async def mock_astream():
            chunks = [
                {"type": "ai", "content": "Hello", "id": "1"},
                {"type": "ai", "content": " world", "id": "1"},
                {"type": "ai", "content": "!", "id": "1"},
            ]
            for chunk in chunks:
                yield chunk

        assistant.astream = mock_astream
        return assistant

    @pytest.fixture
    def conversation_inputs(self):
        """Basic conversation inputs."""
        return ConversationInputs(
            team_id=1,
            user_id=2,
            conversation_id=uuid4(),
            message={"content": "Hello", "type": "human"},
            is_new_conversation=True,
            trace_id="test-trace",
            mode=AssistantMode.ASSISTANT,
        )

    @pytest.mark.asyncio
    async def test_process_conversation_activity_success(
        self,
        conversation_inputs,
        mock_team,
        mock_user,
        mock_conversation,
        mock_redis_client,
        mock_assistant,
    ):
        """Test successful conversation processing."""
        with (
            patch("posthog.temporal.ai.conversation.Team.objects.aget", new=AsyncMock(return_value=mock_team)),
            patch("posthog.temporal.ai.conversation.User.objects.aget", new=AsyncMock(return_value=mock_user)),
            patch(
                "posthog.temporal.ai.conversation.Conversation.objects.aget",
                new=AsyncMock(return_value=mock_conversation),
            ),
            patch("posthog.temporal.ai.conversation.get_async_client", return_value=mock_redis_client),
            patch("posthog.temporal.ai.conversation.Assistant", return_value=mock_assistant),
            patch("posthog.temporal.ai.conversation.AssistantSSESerializer") as mock_serializer,
        ):
            # Mock serializer
            mock_serializer_instance = MagicMock()
            mock_serializer_instance.dumps.return_value = "serialized_chunk"
            mock_serializer.return_value = mock_serializer_instance

            # Execute the activity
            await process_conversation_activity(conversation_inputs)

            # Verify database queries were made (they're patched, so we just check execution completed)

            # Verify Redis operations
            expected_stream_key = f"{CONVERSATION_STREAM_PREFIX}{conversation_inputs.conversation_id}"

            # Should have 4 xadd calls: 3 chunks + 1 completion
            assert mock_redis_client.xadd.call_count == 4

            # Verify all xadd calls have correct maxlen and approximate settings
            for call in mock_redis_client.xadd.call_args_list:
                assert call[1]["maxlen"] == REDIS_STREAM_MAX_LENGTH
                assert call[1]["approximate"] is True

            # Verify completion message
            completion_call = mock_redis_client.xadd.call_args_list[-1]
            assert completion_call[0][0] == expected_stream_key
            assert completion_call[0][1]["status"] == "complete"

            # Verify stream expiration
            mock_redis_client.expire.assert_called_once_with(expected_stream_key, REDIS_STREAM_EXPIRATION_TIME)

            # Verify cleanup
            mock_redis_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_conversation_activity_streaming_error(
        self,
        conversation_inputs,
        mock_team,
        mock_user,
        mock_conversation,
        mock_redis_client,
    ):
        """Test error handling during streaming."""
        # Mock Assistant to raise an error during streaming
        mock_assistant = MagicMock()

        async def mock_astream():
            yield {"type": "ai", "content": "Hello", "id": "1"}
            raise Exception("Streaming error")

        mock_assistant.astream = mock_astream

        with (
            patch("posthog.temporal.ai.conversation.Team.objects.aget", new=AsyncMock(return_value=mock_team)),
            patch("posthog.temporal.ai.conversation.User.objects.aget", new=AsyncMock(return_value=mock_user)),
            patch(
                "posthog.temporal.ai.conversation.Conversation.objects.aget",
                new=AsyncMock(return_value=mock_conversation),
            ),
            patch("posthog.temporal.ai.conversation.get_async_client", return_value=mock_redis_client),
            patch("posthog.temporal.ai.conversation.Assistant", return_value=mock_assistant),
            patch("posthog.temporal.ai.conversation.AssistantSSESerializer") as mock_serializer,
        ):
            # Mock serializer
            mock_serializer_instance = MagicMock()
            mock_serializer_instance.dumps.return_value = "serialized_chunk"
            mock_serializer.return_value = mock_serializer_instance

            # Should raise the streaming error
            with pytest.raises(Exception, match="Streaming error"):
                await process_conversation_activity(conversation_inputs)

            # Should have added error status to stream
            expected_stream_key = f"{CONVERSATION_STREAM_PREFIX}{conversation_inputs.conversation_id}"

            # Should have 2 xadd calls: 1 chunk + 1 error
            assert mock_redis_client.xadd.call_count == 2

            # Verify error message
            error_call = mock_redis_client.xadd.call_args_list[-1]
            assert error_call[0][0] == expected_stream_key
            assert error_call[0][1]["status"] == "error"
            assert error_call[0][1]["error"] == "Streaming error"

            # Error should be raised before Redis cleanup

    @pytest.mark.asyncio
    async def test_process_conversation_activity_no_message(
        self,
        mock_team,
        mock_user,
        mock_conversation,
        mock_redis_client,
        mock_assistant,
    ):
        """Test processing without a message."""
        inputs = ConversationInputs(
            team_id=1,
            user_id=2,
            conversation_id=uuid4(),
            message=None,  # No message
        )

        with (
            patch("posthog.temporal.ai.conversation.Team.objects.aget", new=AsyncMock(return_value=mock_team)),
            patch("posthog.temporal.ai.conversation.User.objects.aget", new=AsyncMock(return_value=mock_user)),
            patch(
                "posthog.temporal.ai.conversation.Conversation.objects.aget",
                new=AsyncMock(return_value=mock_conversation),
            ),
            patch("posthog.temporal.ai.conversation.get_async_client", return_value=mock_redis_client),
            patch("posthog.temporal.ai.conversation.Assistant", return_value=mock_assistant) as mock_assistant_class,
            patch("posthog.temporal.ai.conversation.AssistantSSESerializer") as mock_serializer,
        ):
            # Mock serializer
            mock_serializer_instance = MagicMock()
            mock_serializer_instance.dumps.return_value = "serialized_chunk"
            mock_serializer.return_value = mock_serializer_instance

            # Execute the activity
            await process_conversation_activity(inputs)

            # Verify Assistant was created with None message
            mock_assistant_class.assert_called_once()
            call_args = mock_assistant_class.call_args
            assert call_args[1]["new_message"] is None


class TestUtilityFunctions:
    """Test utility functions."""

    def test_get_conversation_stream_key(self):
        """Test get_conversation_stream_key function."""
        conversation_id = uuid4()
        expected_key = f"{CONVERSATION_STREAM_PREFIX}{conversation_id}"

        result = get_conversation_stream_key(conversation_id)

        assert result == expected_key
