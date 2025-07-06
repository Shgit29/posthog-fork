import asyncio
import structlog
from typing import Optional
from collections.abc import AsyncGenerator
from uuid import UUID

import redis.exceptions as redis_exceptions
from django.conf import settings

from posthog.redis import get_async_client

logger = structlog.get_logger(__name__)

CONVERSATION_STREAM_PREFIX = "conversation_updates:"

# Redis stream configuration
REDIS_STREAM_MAX_LENGTH = 1000  # Maximum number of messages to keep in stream
STREAM_CREATION_WAIT_ATTEMPTS = 10  # Maximum attempts to wait for stream creation (renamed from REDIS_EXPIRATION_TIME)
STREAM_TIMEOUT_SECONDS = 300  # 5 minutes max streaming time


class RedisStreamError(Exception):
    """Raised when a Redis stream read timeout occurs."""

    pass


class RedisStream:
    """Manages conversation streaming from Redis streams."""

    def __init__(self, conversation_id: UUID, stream_key: str, team_id: int):
        self.conversation_id = conversation_id
        self.stream_key = stream_key
        self.redis_client = get_async_client(settings.REDIS_URL)
        self._deletion_lock = asyncio.Lock()
        self._is_deleted = False

    def safe_decode(self, data: bytes, field_name: str) -> Optional[str]:
        """Safely decode Redis stream data with validation."""
        try:
            if not isinstance(data, bytes):
                logger.warning(f"Invalid {field_name} type: {type(data)}", conversation_id=str(self.conversation_id))
                return None

            decoded = data.decode("utf-8")
            return decoded

        except UnicodeDecodeError as e:
            logger.warning(f"Failed to decode {field_name} data: {e}", conversation_id=str(self.conversation_id))
            return None

    async def wait_for_stream_creation(self) -> bool:
        """Wait for stream to be created using exponential backoff."""
        delay = 0.05  # Start with 50ms
        max_delay = 2.0  # Cap at 2 seconds
        max_attempts = STREAM_CREATION_WAIT_ATTEMPTS

        for attempt in range(max_attempts):
            stream_info = await self.get_stream_info()
            if stream_info["exists"]:
                return True

            logger.debug(
                f"Stream not found, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})",
                conversation_id=str(self.conversation_id),
            )
            await asyncio.sleep(delay)

            # Exponential backoff with jitter
            delay = min(delay * 1.5, max_delay)

        return False

    async def read_stream(
        self,
        start_id: str = "0",
        block_ms: int = 100,  # Block for 100ms waiting for new messages
        count: Optional[int] = 1,  # Read one message at a time for true streaming
    ) -> AsyncGenerator[str, None]:
        """
        Read conversation updates from Redis stream.

        Args:
            start_id: Stream ID to start reading from ("0" for beginning, "$" for new messages)
            block_ms: How long to block waiting for new messages (milliseconds)
            count: Maximum number of messages to read

        Yields:
            SSE-formatted chunks for streaming to client
        """
        current_id = start_id
        start_time = asyncio.get_event_loop().time()

        while True:
            # Check for timeout
            if asyncio.get_event_loop().time() - start_time > STREAM_TIMEOUT_SECONDS:
                raise RedisStreamError("Stream timeout - conversation took too long to complete")

            try:
                # Read from stream using async Redis client
                messages = await self.redis_client.xread(
                    {self.stream_key: current_id},
                    block=block_ms,
                    count=count,
                )

                if not messages:
                    # No new messages after blocking, continue polling
                    continue

                for _, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        current_id = message_id

                        # Check for completion or error status
                        if b"status" in fields:
                            status = self.safe_decode(fields[b"status"], "status")
                            if status == "complete":
                                logger.info(
                                    "Stream completed",
                                    conversation_id=str(self.conversation_id),
                                )
                                return
                            elif status == "error":
                                error = self.safe_decode(fields.get(b"error", b"Unknown error"), "error")
                                if error:
                                    raise RedisStreamError(error)
                                continue

                        # Process data chunk
                        if b"data" in fields:
                            data = self.safe_decode(fields[b"data"], "data")
                            if data:
                                yield data

            except redis_exceptions.ConnectionError:
                raise RedisStreamError("Connection lost to conversation stream")
            except redis_exceptions.TimeoutError:
                raise RedisStreamError("Stream read timeout")
            except redis_exceptions.RedisError:
                raise RedisStreamError("Stream read error")
            except Exception:
                raise RedisStreamError("Unexpected error reading conversation stream")

    async def get_stream_history(
        self, start_id: str = "-", end_id: str = "+", count: Optional[int] = None
    ) -> list[tuple[str, dict[bytes, bytes]]]:
        """
        Get historical messages from the stream.

        Args:
            start_id: Start stream ID ("-" for beginning)
            end_id: End stream ID ("+" for end)
            count: Maximum number of messages

        Returns:
            List of (message_id, fields) tuples
        """
        try:
            messages = await self.redis_client.xrange(
                self.stream_key,
                min=start_id,
                max=end_id,
                count=count,
            )
            return messages
        except Exception:
            return []

    async def get_stream_info(self) -> dict:
        """Get stream existence and length in a single pipeline operation."""
        try:
            pipe = self.redis_client.pipeline()
            pipe.exists(self.stream_key)
            pipe.xlen(self.stream_key)
            results = await pipe.execute()

            return {"exists": results[0] > 0, "length": results[1] if results[0] > 0 else 0}
        except Exception:
            return {"exists": False, "length": 0}

    async def delete_stream(self, reason: str = "") -> bool:
        """Delete the Redis stream for this conversation."""
        async with self._deletion_lock:
            if self._is_deleted:
                logger.info(f"Stream already deleted, skipping: {reason}", conversation_id=str(self.conversation_id))
                return True

            try:
                result = await self.redis_client.delete(self.stream_key)
                if result > 0:
                    self._is_deleted = True
                logger.info(
                    f"Deleted conversation stream: {reason}",
                    conversation_id=str(self.conversation_id),
                    deleted=result > 0,
                )
                return result > 0
            except Exception:
                return False
