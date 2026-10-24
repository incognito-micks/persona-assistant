from abc import ABC
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass, field
from typing import Any, Optional

import tiktoken
from openai import AsyncOpenAI, AsyncStream
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionReasoningEffort,
    ChatCompletionToolParam,
)

from approaches.promptmanager import PromptManager
from error import PromptTooLargeError


@dataclass
class ThoughtStep:
    title: str
    description: Optional[Any]
    props: Optional[dict[str, Any]] = None

    def update_token_usage(self, usage: CompletionUsage) -> None:
        if self.props:
            self.props["token_usage"] = TokenUsageProps.from_completion_usage(usage)


@dataclass
class DataPoints:
    text: Optional[list[str]] = None
    images: Optional[list] = None
    citations: Optional[list[str]] = None


@dataclass
class ExtraInfo:
    data_points: DataPoints
    thoughts: list[ThoughtStep] = field(default_factory=list)
    followup_questions: Optional[list[Any]] = None
    answer: Optional[str] = None
    confidence_level: Optional[str] = None
    history_truncated: bool = False


@dataclass
class TokenUsageProps:
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: Optional[int]
    total_tokens: int

    @classmethod
    def from_completion_usage(cls, usage: CompletionUsage) -> "TokenUsageProps":
        return cls(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            reasoning_tokens=(
                usage.completion_tokens_details.reasoning_tokens if usage.completion_tokens_details else None
            ),
            total_tokens=usage.total_tokens,
        )


# GPT reasoning models don't support the same set of parameters as other models
# https://learn.microsoft.com/azure/ai-services/openai/how-to/reasoning
@dataclass
class GPTReasoningModelSupport:
    streaming: bool
    minimal_effort: bool


class Approach(ABC):
    GPT_REASONING_MODELS = {
        "o1": GPTReasoningModelSupport(streaming=False, minimal_effort=False),
        "o3": GPTReasoningModelSupport(streaming=True, minimal_effort=False),
        "o3-mini": GPTReasoningModelSupport(streaming=True, minimal_effort=False),
        "o4-mini": GPTReasoningModelSupport(streaming=True, minimal_effort=False),
        "gpt-5": GPTReasoningModelSupport(streaming=True, minimal_effort=True),
        "gpt-5-nano": GPTReasoningModelSupport(streaming=True, minimal_effort=True),
        "gpt-5-mini": GPTReasoningModelSupport(streaming=True, minimal_effort=True),
    }
    RESPONSE_DEFAULT_TOKEN_LIMIT = 1024
    RESPONSE_REASONING_DEFAULT_TOKEN_LIMIT = 8192
    TOKEN_BUDGET_MARGIN = 1024

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        chatgpt_model: str,
        chatgpt_deployment: Optional[str],
        prompt_manager: PromptManager,
        reasoning_effort: Optional[str] = None,
        chatgpt_context_window: int = 128000,
    ):
        self.openai_client = openai_client
        self.chatgpt_model = chatgpt_model
        self.chatgpt_deployment = chatgpt_deployment
        self.prompt_manager = prompt_manager
        self.reasoning_effort = reasoning_effort
        self.include_token_usage = True
        response_limit = self.get_response_token_limit(chatgpt_model, self.RESPONSE_DEFAULT_TOKEN_LIMIT)
        if (
            type(chatgpt_context_window) is not int
            or chatgpt_context_window <= response_limit + self.TOKEN_BUDGET_MARGIN
        ):
            raise ValueError("Chat context window must exceed the response token reserve and safety margin")
        self.chatgpt_context_window = chatgpt_context_window
        self.token_encoding = tiktoken.encoding_for_model(chatgpt_model)

    def count_message_tokens(self, message: ChatCompletionMessageParam) -> int:
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("Chat context budgeting requires text messages")
        return (
            4
            + len(self.token_encoding.encode(message["role"], disallowed_special=()))
            + len(self.token_encoding.encode(content, disallowed_special=()))
        )

    def fit_messages_to_context(
        self, messages: list[ChatCompletionMessageParam], response_token_limit: int
    ) -> tuple[list[ChatCompletionMessageParam], int]:
        budget = self.chatgpt_context_window - response_token_limit - self.TOKEN_BUDGET_MARGIN
        token_counts = [self.count_message_tokens(message) for message in messages]
        if len(messages) < 2 or token_counts[0] + token_counts[-1] + 3 > budget:
            raise PromptTooLargeError(
                "The required knowledge and latest question exceed the configured model context window. "
                "Shorten the question or reduce the knowledge set."
            )
        total = sum(token_counts) + 3
        history_start = 1
        while total > budget and history_start < len(messages) - 1:
            next_turn = history_start + 1
            while next_turn < len(messages) - 1 and messages[next_turn]["role"] != "user":
                next_turn += 1
            total -= sum(token_counts[history_start:next_turn])
            history_start = next_turn
        return [messages[0], *messages[history_start:]], history_start - 1

    def get_response_token_limit(self, model: str, default_limit: int) -> int:
        if model in self.GPT_REASONING_MODELS:
            return self.RESPONSE_REASONING_DEFAULT_TOKEN_LIMIT
        return default_limit

    def create_chat_completion(
        self,
        chatgpt_deployment: Optional[str],
        chatgpt_model: str,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        response_token_limit: int,
        should_stream: bool = False,
        tools: Optional[list[ChatCompletionToolParam]] = None,
        temperature: Optional[float] = None,
        n: Optional[int] = None,
        reasoning_effort: Optional[ChatCompletionReasoningEffort] = None,
    ) -> Awaitable[ChatCompletion] | Awaitable[AsyncStream[ChatCompletionChunk]]:
        if chatgpt_model in self.GPT_REASONING_MODELS:
            params: dict[str, Any] = {"max_completion_tokens": response_token_limit}
            supported_features = self.GPT_REASONING_MODELS[chatgpt_model]
            if supported_features.streaming and should_stream:
                params["stream"] = True
                params["stream_options"] = {"include_usage": True}
            params["reasoning_effort"] = reasoning_effort or overrides.get("reasoning_effort") or self.reasoning_effort
        else:
            params = {
                "max_tokens": response_token_limit,
                "temperature": temperature or overrides.get("temperature", 0.3),
            }
        if should_stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}

        params["tools"] = tools

        return self.openai_client.chat.completions.create(
            model=chatgpt_deployment if chatgpt_deployment else chatgpt_model,
            messages=messages,
            seed=overrides.get("seed", None),
            n=n or 1,
            **params,
        )

    def format_thought_step_for_chatcompletion(
        self,
        title: str,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        model: str,
        deployment: Optional[str],
        usage: Optional[CompletionUsage] = None,
        reasoning_effort: Optional[ChatCompletionReasoningEffort] = None,
    ) -> ThoughtStep:
        properties: dict[str, Any] = {"model": model}
        if deployment:
            properties["deployment"] = deployment
        if model in self.GPT_REASONING_MODELS:
            properties["reasoning_effort"] = reasoning_effort or overrides.get(
                "reasoning_effort", self.reasoning_effort
            )
        if usage:
            properties["token_usage"] = TokenUsageProps.from_completion_usage(usage)
        return ThoughtStep(title, messages, properties)

    async def run(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def run_stream(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError
