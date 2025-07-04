import pydantic
import structlog
from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from typing import cast

from ee.hogai.api.serializers import ConversationSerializer
from ee.hogai.stream.conversation_stream import ConversationStreamManager
from ee.hogai.graph.graph import AssistantGraph
from ee.models.assistant import Conversation
from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.exceptions import Conflict
from posthog.models.user import User
from posthog.rate_limit import AIBurstRateThrottle, AISustainedRateThrottle
from posthog.schema import HumanMessage
from posthog.utils import get_instance_region


logger = structlog.get_logger(__name__)


class MessageSerializer(serializers.Serializer):
    content = serializers.CharField(required=False, max_length=40000)  ## roughly 10k tokens
    conversation = serializers.UUIDField(
        required=True
    )  # this either retrieves an existing conversation or creates a new one
    contextual_tools = serializers.DictField(required=False, child=serializers.JSONField())
    trace_id = serializers.UUIDField(required=False)  # Only required for new messages
    ui_context = serializers.JSONField(required=False)

    def validate(self, data):
        # If content is provided, this is a new message - validate and create HumanMessage
        if "content" in data:
            if "trace_id" not in data:
                raise serializers.ValidationError("trace_id is required when sending a message.")
            try:
                message_data = {"content": data["content"]}
                if "ui_context" in data:
                    message_data["ui_context"] = data["ui_context"]
                message = HumanMessage.model_validate(message_data)
                data["message"] = message
            except pydantic.ValidationError:
                raise serializers.ValidationError("Invalid message content.")

        return data


class ConversationViewSet(TeamAndOrgViewSetMixin, ListModelMixin, RetrieveModelMixin, GenericViewSet):
    scope_object = "INTERNAL"
    serializer_class = ConversationSerializer
    queryset = Conversation.objects.all()
    lookup_url_kwarg = "conversation"

    def safely_get_queryset(self, queryset):
        # Only allow access to conversations created by the current user
        qs = queryset.filter(user=self.request.user)

        # Allow sending messages to any conversation
        if self.action == "create":
            return qs

        # But retrieval must only return conversations from the assistant and with a title.
        return qs.filter(title__isnull=False, type=Conversation.Type.ASSISTANT).order_by("-updated_at")

    def get_throttles(self):
        if (
            # Do not apply limits in local development
            not settings.DEBUG
            # Only for streaming
            and self.action == "create"
            # Strict limits are skipped for select US region teams (PostHog + an active user we've chatted with)
            and not (get_instance_region() == "US" and self.team_id in (2, 87921))
        ):
            return [AIBurstRateThrottle(), AISustainedRateThrottle()]

        return super().get_throttles()

    def get_serializer_class(self):
        if self.action == "create":
            return MessageSerializer
        return super().get_serializer_class()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["assistant_graph"] = AssistantGraph(self.team, cast(User, self.request.user)).compile_full_graph()
        return context

    def create(self, request: Request, *args, **kwargs):
        """
        Unified endpoint that handles both conversation creation and streaming.

        - If message is provided: Start new conversation processing
        - If no message: Stream from existing conversation
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation_id = serializer.validated_data.get("conversation")

        has_message = "message" in serializer.validated_data

        try:
            self.kwargs[self.lookup_url_kwarg] = conversation_id
            conversation = self.get_object()
        except Exception:
            # Conversation doesn't exist, create it if we have a message
            if not has_message:
                return Response(
                    {"error": "Cannot stream from non-existent conversation"}, status=status.HTTP_400_BAD_REQUEST
                )
            # Use frontend-provided conversation ID
            create_kwargs = {"user": request.user, "team": self.team, "id": conversation_id}
            conversation = self.get_queryset().create(**create_kwargs)

        is_idle = conversation.status == Conversation.Status.IDLE

        stream_manager = ConversationStreamManager(conversation, self.team.id)

        # If this is a streaming request (no new message)
        if not has_message:
            # If conversation is idle (completed), return serialized conversation
            if is_idle:
                serializer = ConversationSerializer(conversation)
                return Response(serializer.data)
            # Otherwise, stream from Redis
            return StreamingHttpResponse(stream_manager.stream_conversation(), content_type="text/event-stream")

        # Check if there's already an active workflow for this conversation
        if not is_idle:
            raise Conflict(
                "Conversation is currently being processed. Please wait for completion before sending new messages."
            )

        # Otherwise, process the new message and stream the response
        return StreamingHttpResponse(
            stream_manager.start_workflow_and_stream(cast(User, request.user).id, serializer.validated_data),
            content_type="text/event-stream",
        )

    @action(detail=True, methods=["PATCH"])
    def cancel(self, request: Request, *args, **kwargs):
        conversation = self.get_object()
        if conversation.status != Conversation.Status.CANCELING:
            conversation.status = Conversation.Status.CANCELING
            conversation.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
