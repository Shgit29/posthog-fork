import json
from unittest.mock import AsyncMock, patch, Mock
from uuid import uuid4

from ee.hogai.stream.conversation_stream import ConversationStreamManager, REDIS_STREAM_MAX_LENGTH
from ee.hogai.stream.redis_stream import RedisStream, RedisStreamError
from ee.hogai.utils.types import AssistantMode
from ee.models.assistant import Conversation
from posthog.constants import MAX_AI_TASK_QUEUE
from posthog.temporal.ai.conversation import ConversationInputs, ConversationWorkflow
from posthog.test.base import BaseTest


class TestConversationStreamManager(BaseTest):
    def setUp(self):
        super().setUp()
        self.conversation = Conversation.objects.create(team=self.team, user=self.user)
        self.team_id = self.team.pk
        self.user_id = self.user.pk
        self.manager = ConversationStreamManager(self.conversation, self.team_id)

    def test_init(self):
        """Test ConversationStreamManager initialization."""
        manager = ConversationStreamManager(self.conversation, self.team_id)

        self.assertEqual(manager.conversation, self.conversation)
        self.assertEqual(manager.team_id, self.team_id)
        self.assertIsInstance(manager.redis_stream, RedisStream)
        self.assertEqual(manager.redis_stream.conversation_id, self.conversation.id)

    @patch("ee.hogai.stream.conversation_stream.connect")
    @patch("ee.hogai.stream.conversation_stream.get_conversation_stream_key")
    async def test_start_workflow_and_stream_success(self, mock_get_stream_key, mock_connect):
        """Test successful workflow start and streaming."""
        # Setup mocks
        mock_get_stream_key.return_value = "test_stream_key"
        mock_client = AsyncMock()
        mock_connect.return_value = mock_client

        # Mock the stream_conversation method
        async def mock_stream_gen():
            for chunk in ["chunk1", "chunk2"]:
                yield chunk

        with patch.object(self.manager, "stream_conversation") as mock_stream:
            mock_stream.return_value = mock_stream_gen()

            validated_data = {
                "message": Mock(model_dump=Mock(return_value={"content": "test"})),
                "contextual_tools": None,
                "conversation": self.conversation.id,
                "trace_id": uuid4(),
            }

            # Call the method
            results = []
            async for chunk in self.manager.start_workflow_and_stream(self.user_id, validated_data, True):
                results.append(chunk)

            # Verify results
            self.assertEqual(results, ["chunk1", "chunk2"])

            # Verify client.start_workflow was called with correct parameters
            mock_client.start_workflow.assert_called_once()
            call_args = mock_client.start_workflow.call_args

            # Check workflow function and inputs
            self.assertEqual(call_args[0][0], ConversationWorkflow.run)
            workflow_inputs = call_args[0][1]
            self.assertIsInstance(workflow_inputs, ConversationInputs)
            self.assertEqual(workflow_inputs.team_id, self.team_id)
            self.assertEqual(workflow_inputs.user_id, self.user_id)
            self.assertEqual(workflow_inputs.conversation_id, self.conversation.id)
            self.assertEqual(workflow_inputs.mode, AssistantMode.ASSISTANT)
            self.assertEqual(workflow_inputs.is_new_conversation, True)

            # Check keyword arguments
            self.assertEqual(call_args[1]["task_queue"], MAX_AI_TASK_QUEUE)
            self.assertIn("conversation-", call_args[1]["id"])

    @patch("ee.hogai.stream.conversation_stream.connect")
    async def test_start_workflow_and_stream_connection_error(self, mock_connect):
        """Test error handling when connection fails."""
        # Setup mock to raise exception
        mock_connect.side_effect = Exception("Connection failed")

        # Mock redis_stream delete method
        with patch.object(self.manager.redis_stream, "delete_stream") as mock_delete:
            mock_delete.return_value = True

            validated_data = {
                "message": Mock(model_dump=Mock(return_value={"content": "test"})),
                "contextual_tools": None,
                "conversation": self.conversation,
                "trace_id": uuid4(),
            }

            # Call the method
            results = []
            async for chunk in self.manager.start_workflow_and_stream(self.user_id, validated_data, True):
                results.append(chunk)

            # Verify failure message is returned
            self.assertEqual(len(results), 1)
            self.assertIn("event: message", results[0])
            self.assertIn("Oops! Something went wrong", results[0])

            # Verify cleanup was attempted
            mock_delete.assert_called_once_with("workflow start failure cleanup")

    async def test_stream_conversation_success(self):
        """Test successful conversation streaming."""
        # Mock redis_stream methods
        with (
            patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait,
            patch.object(self.manager, "_process_historical_messages") as mock_process,
            patch.object(self.manager, "_determine_live_stream_start_id") as mock_determine_id,
            patch.object(self.manager.redis_stream, "read_stream") as mock_read,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks
            async def mock_read_stream(start_id):
                for chunk in ["live_chunk1", "live_chunk2"]:
                    yield chunk

            mock_wait.return_value = True
            mock_process.return_value = (["historical_chunk"], [("id1", {})], False)
            mock_determine_id.return_value = "id1"
            mock_read.side_effect = mock_read_stream
            mock_delete.return_value = True

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify results
            self.assertEqual(results, ["historical_chunk", "live_chunk1", "live_chunk2"])

            # Verify method calls
            mock_wait.assert_called_once()
            mock_process.assert_called_once()
            mock_determine_id.assert_called_once_with([("id1", {})])
            mock_read.assert_called_once_with(start_id="id1")
            mock_delete.assert_called_once_with("stream completed")

    async def test_stream_conversation_stream_not_available(self):
        """Test streaming when stream is not available."""
        with patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait:
            mock_wait.return_value = False

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify failure message is returned
            self.assertEqual(len(results), 1)
            self.assertIn("event: message", results[0])
            self.assertIn("Oops! Something went wrong", results[0])

    async def test_stream_conversation_completed_early(self):
        """Test streaming when conversation is completed during historical processing."""
        with (
            patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait,
            patch.object(self.manager, "_process_historical_messages") as mock_process,
        ):
            # Setup mocks
            mock_wait.return_value = True
            mock_process.return_value = (["historical_chunk"], [], True)  # completed=True

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify only historical chunks are returned
            self.assertEqual(results, ["historical_chunk"])

    async def test_stream_conversation_redis_error(self):
        """Test streaming with Redis error."""
        with (
            patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait,
            patch.object(self.manager, "_process_historical_messages") as mock_process,
            patch.object(self.manager, "_determine_live_stream_start_id") as mock_determine_id,
            patch.object(self.manager.redis_stream, "read_stream") as mock_read,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks
            async def mock_read_stream_error(start_id):
                raise RedisStreamError("Redis error")

            mock_wait.return_value = True
            mock_process.return_value = (["historical_chunk"], [("id1", {})], False)
            mock_determine_id.return_value = "id1"
            mock_read.side_effect = mock_read_stream_error
            mock_delete.return_value = True

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify historical chunk and failure message
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], "historical_chunk")
            self.assertIn("event: message", results[1])
            self.assertIn("Oops! Something went wrong", results[1])

            # Verify cleanup was called
            mock_delete.assert_called_once_with("error cleanup")

    async def test_stream_conversation_general_error(self):
        """Test streaming with general exception."""
        with (
            patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks
            mock_wait.side_effect = Exception("General error")
            mock_delete.return_value = True

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify failure message
            self.assertEqual(len(results), 1)
            self.assertIn("event: message", results[0])
            self.assertIn("Oops! Something went wrong", results[0])

            # Verify cleanup was called
            mock_delete.assert_called_once_with("error cleanup")

    def test_determine_live_stream_start_id_no_history(self):
        """Test determining start ID when no historical messages exist."""
        result = self.manager._determine_live_stream_start_id(None)
        self.assertEqual(result, "0")

        result = self.manager._determine_live_stream_start_id([])
        self.assertEqual(result, "0")

    def test_determine_live_stream_start_id_with_history(self):
        """Test determining start ID with historical messages."""
        historical_messages = [
            ("1234567890-1", {}),
            ("1234567890-0", {}),
            ("1234567891-0", {}),
        ]

        result = self.manager._determine_live_stream_start_id(historical_messages)
        self.assertEqual(result, "1234567891-0")  # Should return the highest ID

    async def test_process_historical_messages_success(self):
        """Test processing historical messages successfully."""
        # Mock historical messages
        historical_messages = [
            ("id1", {b"data": b"chunk1"}),
            ("id2", {b"data": b"chunk2"}),
            ("id3", {b"status": b"complete"}),
        ]

        with (
            patch.object(self.manager.redis_stream, "get_stream_history") as mock_get_history,
            patch.object(self.manager.redis_stream, "safe_decode") as mock_decode,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks
            mock_get_history.return_value = historical_messages
            mock_decode.side_effect = lambda data, field: data.decode() if isinstance(data, bytes) else data
            mock_delete.return_value = True

            # Call the method
            message_chunks, historical_msgs, completed = await self.manager._process_historical_messages()

            # Verify results
            self.assertEqual(message_chunks, ["chunk1", "chunk2"])
            self.assertEqual(historical_msgs, historical_messages)
            self.assertTrue(completed)

            # Verify calls
            mock_get_history.assert_called_once_with(count=REDIS_STREAM_MAX_LENGTH)
            mock_delete.assert_called_once_with("conversation completed")

    async def test_process_historical_messages_with_error(self):
        """Test processing historical messages with error status."""
        historical_messages = [
            ("id1", {b"data": b"chunk1"}),
            ("id2", {b"status": b"error", b"error": b"Test error"}),
        ]

        with (
            patch.object(self.manager.redis_stream, "get_stream_history") as mock_get_history,
            patch.object(self.manager.redis_stream, "safe_decode") as mock_decode,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks
            mock_get_history.return_value = historical_messages
            mock_decode.side_effect = lambda data, field: data.decode() if isinstance(data, bytes) else data
            mock_delete.return_value = True

            # Call the method
            message_chunks, historical_msgs, completed = await self.manager._process_historical_messages()

            # Verify results
            self.assertEqual(len(message_chunks), 2)
            self.assertEqual(message_chunks[0], "chunk1")
            self.assertIn("event: message", message_chunks[1])  # Failure message
            self.assertTrue(completed)

            # Verify cleanup was called
            mock_delete.assert_called_once_with("conversation failed")

    async def test_process_historical_messages_skips_empty_data(self):
        """Test that empty or None data is skipped."""
        historical_messages = [
            ("id1", {b"data": b"valid_chunk"}),
            ("id2", {b"data": b""}),  # Empty data
            ("id3", {b"other": b"not_data"}),  # No data field
        ]

        with (
            patch.object(self.manager.redis_stream, "get_stream_history") as mock_get_history,
            patch.object(self.manager.redis_stream, "safe_decode") as mock_decode,
        ):
            # Setup mocks
            mock_get_history.return_value = historical_messages
            mock_decode.side_effect = lambda data, field: data.decode() if isinstance(data, bytes) and data else None

            # Call the method
            message_chunks, historical_msgs, completed = await self.manager._process_historical_messages()

            # Verify only valid chunk is included
            self.assertEqual(message_chunks, ["valid_chunk"])
            self.assertFalse(completed)

    def test_yield_failure_message(self):
        """Test failure message generation."""
        result = self.manager._yield_failure_message()

        # Verify SSE format
        self.assertIn("event: message", result)
        self.assertIn("data: ", result)
        self.assertIn("Oops! Something went wrong", result)
        self.assertTrue(result.endswith("\n\n"))

        # Verify it's valid JSON in the data field
        data_line = next(line for line in result.split("\n") if line.startswith("data: "))
        json_data = data_line.replace("data: ", "")
        parsed = json.loads(json_data)

        self.assertEqual(parsed["content"], "Oops! Something went wrong. Please try again.")
        self.assertIn("id", parsed)

    async def test_stream_conversation_filters_empty_chunks(self):
        """Test that empty message chunks are filtered out."""
        # Mock the original method to test filtering
        with (
            patch.object(self.manager.redis_stream, "wait_for_stream_creation") as mock_wait,
            patch.object(self.manager, "_process_historical_messages") as mock_process,
            patch.object(self.manager, "_determine_live_stream_start_id") as mock_determine_id,
            patch.object(self.manager.redis_stream, "read_stream") as mock_read,
            patch.object(self.manager.redis_stream, "delete_stream") as mock_delete,
        ):
            # Setup mocks with empty chunks
            async def mock_read_stream_empty(start_id):
                return
                yield  # This is unreachable but keeps the function as a generator

            mock_wait.return_value = True
            mock_process.return_value = (["", "valid_chunk", None, "another_chunk"], [], False)
            mock_determine_id.return_value = "0"
            mock_read.side_effect = mock_read_stream_empty
            mock_delete.return_value = True

            # Call the method
            results = []
            async for chunk in self.manager.stream_conversation():
                results.append(chunk)

            # Verify only non-empty chunks are returned
            self.assertEqual(results, ["valid_chunk", "another_chunk"])
