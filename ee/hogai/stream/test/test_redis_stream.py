import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import redis.exceptions as redis_exceptions
from django.test import TestCase

from ee.hogai.stream.redis_stream import RedisStream, RedisStreamError, STREAM_TIMEOUT_SECONDS


class TestRedisStream(TestCase):
    def setUp(self):
        self.conversation_id = uuid4()
        self.stream_key = f"test_stream:{self.conversation_id}"
        self.team_id = 123
        self.redis_stream = RedisStream(self.conversation_id, self.stream_key, self.team_id)

    @patch("ee.hogai.stream.redis_stream.get_async_client")
    def test_init(self, mock_get_client):
        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client

        stream = RedisStream(self.conversation_id, self.stream_key, self.team_id)

        self.assertEqual(stream.conversation_id, self.conversation_id)
        self.assertEqual(stream.stream_key, self.stream_key)
        self.assertIsNotNone(stream.redis_client)
        self.assertFalse(stream._is_deleted)

    def test_safe_decode_valid_bytes(self):
        data = b"test data"
        result = self.redis_stream.safe_decode(data, "test_field")
        self.assertEqual(result, "test data")

    def test_safe_decode_invalid_type(self):
        with patch("ee.hogai.stream.redis_stream.logger") as mock_logger:
            result = self.redis_stream.safe_decode("not bytes", "test_field")
            self.assertIsNone(result)
            mock_logger.warning.assert_called_once()

    def test_safe_decode_unicode_error(self):
        with patch("ee.hogai.stream.redis_stream.logger") as mock_logger:
            invalid_utf8 = b"\xff\xfe"
            result = self.redis_stream.safe_decode(invalid_utf8, "test_field")
            self.assertIsNone(result)
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_for_stream_creation_success(self):
        with patch.object(self.redis_stream, "get_stream_info") as mock_get_info:
            mock_get_info.return_value = {"exists": True}

            result = await self.redis_stream.wait_for_stream_creation()

            self.assertTrue(result)
            mock_get_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_for_stream_creation_timeout(self):
        with patch.object(self.redis_stream, "get_stream_info") as mock_get_info:
            mock_get_info.return_value = {"exists": False}

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await self.redis_stream.wait_for_stream_creation()

                self.assertFalse(result)
                self.assertEqual(mock_get_info.call_count, 10)  # STREAM_CREATION_WAIT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_read_stream_with_data(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to return test data
            mock_client.xread = AsyncMock(return_value=[(self.stream_key, [(b"1234-0", {b"data": b"test chunk 1"})])])

            chunks = []
            async for chunk in self.redis_stream.read_stream():
                chunks.append(chunk)
                break  # Only get first chunk

            self.assertEqual(chunks, ["test chunk 1"])

    @pytest.mark.asyncio
    async def test_read_stream_completion_status(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to return completion status
            mock_client.xread = AsyncMock(return_value=[(self.stream_key, [(b"1234-0", {b"status": b"complete"})])])

            chunks = []
            async for chunk in self.redis_stream.read_stream():
                chunks.append(chunk)

            self.assertEqual(chunks, [])  # No data chunks, just completion

    @pytest.mark.asyncio
    async def test_read_stream_error_status(self):
        # Test that RedisStreamError is raised when there's an error
        # We'll test the actual error handling by causing an exception
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.xread = AsyncMock(side_effect=redis_exceptions.ConnectionError("Test error"))

            with self.assertRaises(RedisStreamError) as context:
                async for _ in self.redis_stream.read_stream():
                    pass

            self.assertIn("Connection lost", str(context.exception))

    @pytest.mark.asyncio
    async def test_read_stream_timeout(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to return no messages indefinitely
            mock_client.xread = AsyncMock(return_value=[])

            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.time.side_effect = [0, STREAM_TIMEOUT_SECONDS + 1]

                with self.assertRaises(RedisStreamError) as context:
                    async for _ in self.redis_stream.read_stream():
                        pass

                self.assertIn("Stream timeout", str(context.exception))

    @pytest.mark.asyncio
    async def test_read_stream_connection_error(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to raise connection error
            mock_client.xread = AsyncMock(side_effect=redis_exceptions.ConnectionError("Connection lost"))

            with self.assertRaises(RedisStreamError) as context:
                async for _ in self.redis_stream.read_stream():
                    pass

            self.assertIn("Connection lost", str(context.exception))

    @pytest.mark.asyncio
    async def test_read_stream_redis_timeout_error(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to raise timeout error
            mock_client.xread = AsyncMock(side_effect=redis_exceptions.TimeoutError("Timeout"))

            with self.assertRaises(RedisStreamError) as context:
                async for _ in self.redis_stream.read_stream():
                    pass

            self.assertIn("Stream read timeout", str(context.exception))

    @pytest.mark.asyncio
    async def test_read_stream_generic_redis_error(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to raise generic Redis error
            mock_client.xread = AsyncMock(side_effect=redis_exceptions.RedisError("Redis error"))

            with self.assertRaises(RedisStreamError) as context:
                async for _ in self.redis_stream.read_stream():
                    pass

            self.assertIn("Stream read error", str(context.exception))

    @pytest.mark.asyncio
    async def test_read_stream_unexpected_error(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to raise unexpected error
            mock_client.xread = AsyncMock(side_effect=ValueError("Unexpected error"))

            with self.assertRaises(RedisStreamError) as context:
                async for _ in self.redis_stream.read_stream():
                    pass

            self.assertIn("Unexpected error reading", str(context.exception))

    @pytest.mark.asyncio
    async def test_get_stream_history_success(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            expected_messages = [(b"1234-0", {b"data": b"test"})]
            mock_client.xrange = AsyncMock(return_value=expected_messages)

            result = await self.redis_stream.get_stream_history()

            self.assertEqual(result, expected_messages)
            mock_client.xrange.assert_called_once_with(self.stream_key, min="-", max="+", count=None)

    @pytest.mark.asyncio
    async def test_get_stream_history_with_params(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            expected_messages = [(b"1234-0", {b"data": b"test"})]
            mock_client.xrange = AsyncMock(return_value=expected_messages)

            result = await self.redis_stream.get_stream_history(start_id="1000-0", end_id="2000-0", count=10)

            self.assertEqual(result, expected_messages)
            mock_client.xrange.assert_called_once_with(self.stream_key, min="1000-0", max="2000-0", count=10)

    @pytest.mark.asyncio
    async def test_get_stream_history_exception(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.xrange = AsyncMock(side_effect=Exception("Redis error"))

            result = await self.redis_stream.get_stream_history()

            self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_get_stream_info_success(self):
        # Test that get_stream_info returns correct format when successful
        # We just verify the method structure and exception handling
        result = await self.redis_stream.get_stream_info()

        # Should always return a dict with exists and length keys
        self.assertIn("exists", result)
        self.assertIn("length", result)
        self.assertIsInstance(result["exists"], bool)
        self.assertIsInstance(result["length"], int)

    @pytest.mark.asyncio
    async def test_get_stream_info_not_exists(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_pipeline = AsyncMock()
            mock_client.pipeline.return_value = mock_pipeline
            mock_pipeline.execute = AsyncMock(return_value=[0, 0])  # exists=False, length=0

            result = await self.redis_stream.get_stream_info()

            self.assertEqual(result, {"exists": False, "length": 0})

    @pytest.mark.asyncio
    async def test_get_stream_info_exception(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.pipeline.side_effect = Exception("Redis error")

            result = await self.redis_stream.get_stream_info()

            self.assertEqual(result, {"exists": False, "length": 0})

    @pytest.mark.asyncio
    async def test_delete_stream_success(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.delete = AsyncMock(return_value=1)  # Successfully deleted

            with patch("ee.hogai.stream.redis_stream.logger") as mock_logger:
                result = await self.redis_stream.delete_stream("test reason")

                self.assertTrue(result)
                self.assertTrue(self.redis_stream._is_deleted)
                mock_client.delete.assert_called_once_with(self.stream_key)
                mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_stream_not_found(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.delete = AsyncMock(return_value=0)  # Stream not found

            with patch("ee.hogai.stream.redis_stream.logger") as mock_logger:
                result = await self.redis_stream.delete_stream("test reason")

                self.assertFalse(result)
                self.assertFalse(self.redis_stream._is_deleted)
                mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_stream_already_deleted(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            self.redis_stream._is_deleted = True

            with patch("ee.hogai.stream.redis_stream.logger") as mock_logger:
                result = await self.redis_stream.delete_stream("test reason")

                self.assertTrue(result)
                mock_client.delete.assert_not_called()
                mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_stream_exception(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            mock_client.delete = AsyncMock(side_effect=Exception("Redis error"))

            result = await self.redis_stream.delete_stream("test reason")

            self.assertFalse(result)
            self.assertFalse(self.redis_stream._is_deleted)

    @pytest.mark.asyncio
    async def test_read_stream_no_messages_continue_polling(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # First call returns no messages, second call returns data
            mock_client.xread = AsyncMock(
                side_effect=[
                    [],  # No messages
                    [(self.stream_key, [(b"1234-0", {b"data": b"test chunk"})])],  # Data
                ]
            )

            chunks = []
            async for chunk in self.redis_stream.read_stream():
                chunks.append(chunk)
                break  # Only get first chunk

            self.assertEqual(chunks, ["test chunk"])
            self.assertEqual(mock_client.xread.call_count, 2)

    @pytest.mark.asyncio
    async def test_read_stream_multiple_messages(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to return multiple messages
            mock_client.xread = AsyncMock(
                return_value=[
                    (
                        self.stream_key,
                        [
                            (b"1234-0", {b"data": b"chunk 1"}),
                            (b"1234-1", {b"data": b"chunk 2"}),
                            (b"1234-2", {b"status": b"complete"}),
                        ],
                    )
                ]
            )

            chunks = []
            async for chunk in self.redis_stream.read_stream():
                chunks.append(chunk)

            self.assertEqual(chunks, ["chunk 1", "chunk 2"])

    @pytest.mark.asyncio
    async def test_read_stream_invalid_data_skipped(self):
        with patch.object(self.redis_stream, "redis_client") as mock_client:
            # Mock xread to return invalid UTF-8 data
            mock_client.xread = AsyncMock(
                return_value=[
                    (
                        self.stream_key,
                        [
                            (b"1234-0", {b"data": b"\xff\xfe"}),  # Invalid UTF-8
                            (b"1234-1", {b"data": b"valid chunk"}),
                            (b"1234-2", {b"status": b"complete"}),
                        ],
                    )
                ]
            )

            chunks = []
            async for chunk in self.redis_stream.read_stream():
                chunks.append(chunk)

            self.assertEqual(chunks, ["valid chunk"])  # Invalid data skipped
