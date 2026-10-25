import logging
import re
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import asdict
from typing import Any, Optional, cast

from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionUserMessageParam,
)

from approaches.approach import (
    Approach,
    DataPoints,
    ExtraInfo,
    ThoughtStep,
)
from approaches.promptmanager import PromptManager
from core.knowledgebase import render_knowledge_sources
from core.profile import build_persona_instructions


class PersonaChatApproach(Approach):
    """
    Chat approach that injects pre-loaded knowledge base content directly into the LLM prompt.
    No search index or embeddings required — all knowledge files are loaded at startup.
    """

    CONVERSATIONAL_PATTERNS = [
        r"^(hi|hello|hey|howdy|hiya|yo|sup|greetings|good\s*(morning|afternoon|evening|day|night))[\s!?.,]*$",
        r"^(thanks?|thank\s*you|thx|ty|cheers|appreciate\s*it)[\s!?.,]*$",
        r"^(bye|goodbye|see\s*ya|later|take\s*care|have\s*a\s*good\s*(day|one|night))[\s!?.,]*$",
        r"^(who\s*are\s*you|what\s*are\s*you|what\s*can\s*you\s*do|how\s*are\s*you|what'?s?\s*up|how'?s?\s*it\s*going)[\s!?.,]*$",
        r"^(ok|okay|cool|nice|great|got\s*it|understood|sure|yeah|yep|yes|no|nope|alright|awesome|perfect|wonderful)[\s!?.,]*$",
        r"^(lol|haha|hehe|rofl|lmao)[\s!?.,]*$",
    ]

    SMALL_TALK_PATTERNS = [
        r"\b(weather|forecast)\b",
        r"\b(joke|funny|humor|laugh)\b",
        r"\b(tell\s*me\s*(a|about)\s*(joke|story))\b",
        r"\b(what\s*time|what\s*day)\b",
        r"\b(bored|lonely|chat\s*with\s*me|let'?s?\s*talk)\b",
        r"\b(recommend|suggestion)\b.*\b(movie|book|song|show)\b",
        r"\b(who\s*made\s*you|who\s*built\s*you|who\s*created\s*you)\b",
        r"\b(meaning\s*of\s*life)\b",
        r"\b(what'?s?\s*your\s*(name|favorite|opinion))\b",
    ]

    ADMIN_ANSWERABLE_PATTERNS = [
        r"\bfun\s*fact",
        r"\binteresting.{0,20}\babout\b",
        r"\bsomething.{0,20}\babout.{0,20}\bpersonally\b",
        r"\btell\s*me\s*about\s*(him|her|himself|herself|yourself)\b",
        r"\bcontact\b",
        r"\bemail\b",
        r"\be-?mail\s*(address|id)\b",
        r"\breach\s*(out|him|her)\b",
        r"\bget\s*in\s*touch\b",
        r"\bphone\b",
        r"\bcall\b",
        r"\bbook\b.*\b(time|meeting|slot|chat)\b",
        r"\bschedule\b",
        r"\bcalendar\b",
    ]

    @staticmethod
    def is_conversational_query(query: str) -> bool:
        cleaned = query.strip().lower()
        if len(cleaned.split()) > 8:
            return False
        for pattern in PersonaChatApproach.CONVERSATIONAL_PATTERNS:
            if re.match(pattern, cleaned, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def is_small_talk(query: str) -> bool:
        cleaned = query.strip().lower()
        if len(cleaned.split()) > 15:
            return False
        for pattern in PersonaChatApproach.SMALL_TALK_PATTERNS:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def is_admin_answerable(query: str) -> bool:
        cleaned = query.strip().lower()
        for pattern in PersonaChatApproach.ADMIN_ANSWERABLE_PATTERNS:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return True
        return False

    def __init__(
        self,
        *,
        openai_client: AsyncOpenAI,
        chatgpt_model: str,
        chatgpt_deployment: Optional[str],
        prompt_manager: PromptManager,
        reasoning_effort: Optional[str] = None,
        knowledge_sources: Optional[list[str]] = None,
        chatgpt_context_window: int = 128000,
    ):
        super().__init__(
            openai_client=openai_client,
            chatgpt_model=chatgpt_model,
            chatgpt_deployment=chatgpt_deployment,
            prompt_manager=prompt_manager,
            reasoning_effort=reasoning_effort,
            chatgpt_context_window=chatgpt_context_window,
        )
        self.work_answer_prompt = self.prompt_manager.load_prompt("persona_answer.prompty")
        self.knowledge_sources: list[str] = knowledge_sources or []

    def extract_followup_questions(self, content: Optional[str]):
        if content is None:
            return content, []
        return content.split("<<")[0], re.findall(r"<<([^>>]+)>>", content)

    async def run_without_streaming(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        session_state: Any = None,
        admin_settings: dict[str, Any] = {},
    ) -> dict[str, Any]:
        extra_info, chat_coroutine = await self.run_until_final_call(
            messages, overrides, auth_claims, should_stream=False, admin_settings=admin_settings
        )
        chat_completion_response: ChatCompletion = await cast(Awaitable[ChatCompletion], chat_coroutine)
        content = chat_completion_response.choices[0].message.content
        role = chat_completion_response.choices[0].message.role
        if overrides.get("suggest_followup_questions"):
            content, followup_questions = self.extract_followup_questions(content)
            extra_info.followup_questions = followup_questions
        if self.include_token_usage and extra_info.thoughts and chat_completion_response.usage:
            extra_info.thoughts[-1].update_token_usage(chat_completion_response.usage)
        chat_app_response = {
            "message": {"content": content, "role": role},
            "context": {
                "thoughts": extra_info.thoughts,
                "data_points": {
                    key: value for key, value in asdict(extra_info.data_points).items() if value is not None
                },
                "followup_questions": extra_info.followup_questions,
                "confidence_level": extra_info.confidence_level,
            },
            "session_state": session_state,
        }
        if extra_info.history_truncated:
            chat_app_response["context"]["history_truncated"] = True
        return chat_app_response

    async def run_with_streaming(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        session_state: Any = None,
        admin_settings: dict[str, Any] = {},
        prepared_call: Optional[
            tuple[ExtraInfo, Awaitable[ChatCompletion] | Awaitable[AsyncStream[ChatCompletionChunk]]]
        ] = None,
    ) -> AsyncGenerator[dict, None]:
        if prepared_call is None:
            prepared_call = await self.run_until_final_call(
                messages, overrides, auth_claims, should_stream=True, admin_settings=admin_settings
            )
        extra_info, chat_coroutine = prepared_call
        yield {"delta": {"role": "assistant"}, "context": extra_info, "session_state": session_state}

        followup_questions_started = False
        followup_content = ""
        chat_result = await chat_coroutine

        if isinstance(chat_result, ChatCompletion):
            message = chat_result.choices[0].message
            content = message.content or ""
            role = message.role or "assistant"

            followup_questions: list[str] = []
            if overrides.get("suggest_followup_questions"):
                content, followup_questions = self.extract_followup_questions(content)
                extra_info.followup_questions = followup_questions

            if self.include_token_usage and extra_info.thoughts and chat_result.usage:
                extra_info.thoughts[-1].update_token_usage(chat_result.usage)

            delta_payload: dict[str, Any] = {"role": role}
            if content:
                delta_payload["content"] = content
            yield {"delta": delta_payload}

            yield {"delta": {"role": "assistant"}, "context": extra_info, "session_state": session_state}

            if followup_questions:
                yield {
                    "delta": {"role": "assistant"},
                    "context": {"context": extra_info, "followup_questions": followup_questions},
                }
            return

        chat_result = cast(AsyncStream[ChatCompletionChunk], chat_result)

        async for event_chunk in chat_result:
            event = event_chunk.model_dump()
            if event["choices"]:
                completion = {
                    "delta": {
                        "content": event["choices"][0]["delta"].get("content"),
                        "role": event["choices"][0]["delta"]["role"],
                    }
                }
                delta_content_raw = completion["delta"].get("content")
                delta_content: str = delta_content_raw or ""
                if overrides.get("suggest_followup_questions") and "<<" in delta_content:
                    followup_questions_started = True
                    earlier_content = delta_content[: delta_content.index("<<")]
                    if earlier_content:
                        completion["delta"]["content"] = earlier_content
                        yield completion
                    followup_content += delta_content[delta_content.index("<<") :]
                elif followup_questions_started:
                    followup_content += delta_content
                else:
                    yield completion
            else:
                if event_chunk.usage and extra_info.thoughts and self.include_token_usage:
                    extra_info.thoughts[-1].update_token_usage(event_chunk.usage)
                    yield {"delta": {"role": "assistant"}, "context": extra_info, "session_state": session_state}

        if followup_content:
            _, followup_questions = self.extract_followup_questions(followup_content)
            yield {
                "delta": {"role": "assistant"},
                "context": {"context": extra_info, "followup_questions": followup_questions},
            }

    async def run(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> dict[str, Any]:
        overrides = context.get("overrides", {})
        auth_claims = context.get("auth_claims", {})
        admin_settings = context.get("admin_settings", {})
        return await self.run_without_streaming(messages, overrides, auth_claims, session_state, admin_settings)

    async def run_stream(
        self,
        messages: list[ChatCompletionMessageParam],
        session_state: Any = None,
        context: dict[str, Any] = {},
    ) -> AsyncGenerator[dict[str, Any], None]:
        overrides = context.get("overrides", {})
        auth_claims = context.get("auth_claims", {})
        admin_settings = context.get("admin_settings", {})
        prepared_call = await self.run_until_final_call(
            messages, overrides, auth_claims, should_stream=True, admin_settings=admin_settings
        )
        return self.run_with_streaming(messages, overrides, auth_claims, session_state, admin_settings, prepared_call)

    async def run_until_final_call(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: bool = False,
        admin_settings: dict[str, Any] = {},
    ) -> tuple[ExtraInfo, Awaitable[ChatCompletion] | Awaitable[AsyncStream[ChatCompletionChunk]]]:
        original_user_query = messages[-1]["content"]

        # URL extraction and JD detection
        jd_text: str | None = None
        fetched_url: str | None = None
        if isinstance(original_user_query, str):
            from core.urlfetcher import extract_url_content, find_urls

            urls = find_urls(original_user_query)
            if urls:
                fetched_content = await extract_url_content(urls[0])
                if fetched_content:
                    remaining_text = original_user_query.replace(urls[0], "").strip()
                    original_user_query = (
                        f"{remaining_text}\n\n--- Job Description ---\n{fetched_content}"
                        if remaining_text
                        else f"--- Job Description ---\n{fetched_content}"
                    )
                    jd_text = fetched_content
                    fetched_url = urls[0]
                    logging.getLogger(__name__).info(
                        "Fetched a requested job description (%d chars)", len(fetched_content)
                    )
                else:
                    logging.getLogger(__name__).warning("Unable to fetch a requested URL")

            if not jd_text and len(original_user_query) > 300:
                jd_text = original_user_query

            if jd_text:
                messages = list(messages)
                messages[-1] = {**cast(ChatCompletionUserMessageParam, messages[-1]), "content": original_user_query}

        is_conversational = isinstance(original_user_query, str) and self.is_conversational_query(original_user_query)

        is_small_talk_query = (
            not is_conversational and isinstance(original_user_query, str) and self.is_small_talk(original_user_query)
        )
        is_admin_answerable_query = (
            not is_conversational
            and not is_small_talk_query
            and isinstance(original_user_query, str)
            and self.is_admin_answerable(original_user_query)
        )

        reasoning_model_support = self.GPT_REASONING_MODELS.get(self.chatgpt_model)
        if reasoning_model_support and (not reasoning_model_support.streaming and should_stream):
            raise Exception(
                f"{self.chatgpt_model} does not support streaming. Please use a different model or disable streaming."
            )

        if is_conversational:
            extra_info = ExtraInfo(
                DataPoints(text=self.knowledge_sources, images=[], citations=[]),
                thoughts=[
                    ThoughtStep(
                        "Conversational query detected",
                        original_user_query,
                        {"note": "Using a conversational tone while retaining factual knowledge."},
                    )
                ],
                confidence_level="high",
            )
        elif is_small_talk_query:
            extra_info = ExtraInfo(
                DataPoints(text=self.knowledge_sources, images=[], citations=[]),
                thoughts=[
                    ThoughtStep(
                        "Small talk detected",
                        original_user_query,
                        {"note": "Casual conversation — responding naturally."},
                    )
                ],
                confidence_level="small_talk",
            )
        elif is_admin_answerable_query:
            extra_info = ExtraInfo(
                DataPoints(text=self.knowledge_sources, images=[], citations=[]),
                thoughts=[
                    ThoughtStep(
                        "Admin-answerable query detected",
                        original_user_query,
                        {"note": "Profile settings and knowledge are both available for this question."},
                    )
                ],
                confidence_level="admin_info",
            )
        else:
            extra_info = self.run_direct_context_approach()

        if extra_info.data_points.text is not None:
            extra_info.data_points.text = render_knowledge_sources(extra_info.data_points.text)

        # Add thought step for URL fetch if applicable
        if fetched_url and jd_text:
            extra_info.thoughts.insert(
                0,
                ThoughtStep(
                    "Fetched job description from URL",
                    fetched_url,
                    {"extracted_chars": len(jd_text)},
                ),
            )

        # Build confidence note
        confidence_note = ""
        if is_conversational:
            confidence_note = (
                "This may be a greeting or acknowledgement. Respond naturally and warmly in character, "
                "using the supplied knowledge for any factual claims."
            )
        elif extra_info.confidence_level == "small_talk":
            confidence_note = (
                "This may include casual conversation or small talk. "
                "Respond naturally, warmly, and in character as the owner's AI assistant. "
                "Use the supplied knowledge for any factual claims about the owner. "
                "If relevant, you can mention that you're primarily designed to answer questions about the owner's work."
            )
        elif extra_info.confidence_level == "admin_info":
            confidence_note = (
                "This question may concern the owner's profile information "
                "(fun facts, contact details, email addresses, calendar booking link). "
                "Use profile information and the supplied knowledge as appropriate."
            )

        answer_prompt = self.work_answer_prompt

        messages = self.prompt_manager.render_prompt(
            answer_prompt,
            {
                "include_follow_up_questions": bool(overrides.get("suggest_followup_questions")),
                "past_messages": messages[:-1],
                "user_query": original_user_query,
                "text_sources": extra_info.data_points.text,
                "image_sources": extra_info.data_points.images,
                "citations": extra_info.data_points.citations,
                "owner_name": admin_settings.get("owner_name", ""),
                "persona_instructions": build_persona_instructions(admin_settings),
                "fun_facts": admin_settings.get("fun_facts", []),
                "vacation_mode": admin_settings.get("vacation_mode", False),
                "vacation_return_date": admin_settings.get("vacation_return_date"),
                "owner_email": admin_settings.get("owner_email", ""),
                "manager_email": admin_settings.get("manager_email", ""),
                "calendar_url": admin_settings.get("calendar_url", ""),
                "confidence_note": confidence_note,
                "jd_mode": jd_text is not None,
                "jd_text": jd_text or "",
            },
        )

        response_token_limit = self.get_response_token_limit(self.chatgpt_model, 1024)
        messages, removed_messages = self.fit_messages_to_context(messages, response_token_limit)
        if removed_messages:
            extra_info.history_truncated = True
            logging.getLogger(__name__).info(
                "Omitted %d older messages to fit the chat context window", removed_messages
            )
        chat_coroutine = cast(
            Awaitable[ChatCompletion] | Awaitable[AsyncStream[ChatCompletionChunk]],
            self.create_chat_completion(
                self.chatgpt_deployment,
                self.chatgpt_model,
                messages,
                overrides,
                response_token_limit,
                should_stream,
            ),
        )
        extra_info.thoughts.append(
            self.format_thought_step_for_chatcompletion(
                title="Prompt to generate answer",
                messages=messages,
                overrides=overrides,
                model=self.chatgpt_model,
                deployment=self.chatgpt_deployment,
                usage=None,
            )
        )
        return (extra_info, chat_coroutine)

    def run_direct_context_approach(self) -> ExtraInfo:
        """Inject pre-loaded knowledge base directly as context, bypassing search."""
        return ExtraInfo(
            DataPoints(text=self.knowledge_sources, images=[], citations=[]),
            thoughts=[
                ThoughtStep(
                    "Direct context injection",
                    f"{len(self.knowledge_sources)} knowledge files loaded",
                    {"note": "All knowledge injected directly — no search index used."},
                )
            ],
            confidence_level="high",
        )
