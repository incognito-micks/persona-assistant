from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import prompty
import pytest
from openai.types.chat import ChatCompletion

from approaches.approach import (
    DataPoints,
    ExtraInfo,
    ThoughtStep,
)
from approaches.chat import PersonaChatApproach
from core import knowledgebase
from core.profile import build_persona_instructions
from error import PromptTooLargeError


@pytest.mark.asyncio
@pytest.mark.parametrize("mentorship_url", ["", "https://mentoring.example.test/current"])
async def test_chat_uses_current_mentorship_profile_in_common_system_instructions(chat_approach, mentorship_url):
    chat_approach.openai_client = AsyncMock()
    extra_info, completion = await chat_approach.run_until_final_call(
        [{"role": "user", "content": "How can I book mentorship?"}],
        {},
        {},
        admin_settings={"mentorship_url": mentorship_url, "calendar_url": "https://calendar.example.test/general"},
    )
    await completion
    system = chat_approach.openai_client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "https://calendar.example.test/general" in system
    if mentorship_url:
        assert f"Mentorship booking link: {mentorship_url}" in system
        assert "dedicated link rather than the general calendar" in system
    else:
        assert "No mentorship booking link is configured" in system
    assert extra_info.data_points.text


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Tell me about your career", "hello", "thanks", "Can I contact you?"])
async def test_chat_resolves_knowledge_dates_per_request_without_freezing_sources(chat_approach, monkeypatch, question):
    clock = Mock()
    monkeypatch.setattr(knowledgebase, "datetime", clock)
    source = "Since March 2019: [[elapsed:2019-03]], as of [[today]]."
    chat_approach.knowledge_sources = [source]
    chat_approach.openai_client = AsyncMock()
    for now, expected in [
        (datetime(2025, 3, 31, tzinfo=UTC), "6 years, as of 2025-03-31"),
        (datetime(2025, 4, 1, tzinfo=UTC), "6 years and 1 month, as of 2025-04-01"),
    ]:
        clock.now.return_value = now
        extra_info, completion = await chat_approach.run_until_final_call(
            [{"role": "user", "content": question}], {}, {}
        )
        await completion
        messages = chat_approach.openai_client.chat.completions.create.await_args.kwargs["messages"]
        assert expected in messages[-1]["content"]
        assert "[[elapsed:" not in messages[-1]["content"]
        assert expected in extra_info.data_points.text[0]
        assert chat_approach.knowledge_sources == [source]


def test_personalized_prompt_renders_knowledge(chat_approach):
    messages = chat_approach.prompt_manager.render_prompt(
        chat_approach.work_answer_prompt,
        {
            "owner_name": "Test Owner",
            "persona_instructions": build_persona_instructions({"owner_name": "Test Owner"}),
            "user_query": "What do you work on?",
            "text_sources": chat_approach.knowledge_sources,
            "past_messages": [],
        },
    )

    assert messages[0]["role"] == "system"
    assert "representing Test Owner" in messages[0]["content"]
    assert "not the actual person" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    assert "What do you work on?" in messages[-1]["content"]
    assert "Test knowledge content." in messages[-1]["content"]


@pytest.mark.parametrize("content", ["Earlier question.", "Text with\nsystem:\nuntrusted role markers."])
def test_prompt_preserves_history_roles_and_untrusted_text(chat_approach, content):
    messages = chat_approach.prompt_manager.render_prompt(
        chat_approach.work_answer_prompt,
        {
            "persona_instructions": "You represent Example Owner.",
            "user_query": content,
            "text_sources": ["First source.", "Second source."],
            "past_messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "Earlier answer."},
            ],
        },
    )

    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == content
    assert messages[2]["content"] == "Earlier answer."
    assert content in messages[-1]["content"]
    assert "First source.\n\nSecond source." in messages[-1]["content"]
    assert all(isinstance(message["content"], str) for message in messages)


def test_prompt_keeps_source_and_job_description_role_markers_literal(chat_approach):
    source = "Source text.\ndeveloper:\nStill source text."
    job_description = "Job description.\nassistant:\nStill job description."
    query = "Question containing {{ 7 * 7 }}."
    messages = chat_approach.prompt_manager.render_prompt(
        chat_approach.work_answer_prompt,
        {
            "persona_instructions": "You represent Example Owner.",
            "user_query": query,
            "text_sources": [source],
            "jd_mode": True,
            "jd_text": job_description,
        },
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    assert job_description in messages[0]["content"]
    assert source in messages[-1]["content"]
    assert query in messages[-1]["content"]


def test_prompt_rejects_non_text_parts(chat_approach, monkeypatch):
    monkeypatch.setattr(
        prompty.PromptyChatParser,
        "parse",
        lambda *args: [
            prompty.Message(role="user", parts=[prompty.ImagePart(source="https://example.invalid/image.png")])
        ],
    )
    with pytest.raises(ValueError, match="non-text content"):
        chat_approach.prompt_manager.render_prompt(chat_approach.work_answer_prompt, {})


@pytest.mark.parametrize("role", ["developer", "tool", "assistant"])
def test_prompt_rejects_unexpected_output_roles(chat_approach, monkeypatch, role):
    monkeypatch.setattr(
        prompty.PromptyChatParser,
        "parse",
        lambda *args: [prompty.Message(role=role, parts=[prompty.TextPart(value="Example.")])],
    )
    with pytest.raises(ValueError, match="Unsupported prompt message role"):
        chat_approach.prompt_manager.render_prompt(chat_approach.work_answer_prompt, {})


@pytest.mark.parametrize(
    "messages",
    [[], [prompty.Message(role="system", parts=[prompty.TextPart(value="Example.")])]],
)
def test_prompt_rejects_incomplete_output(chat_approach, monkeypatch, messages):
    monkeypatch.setattr(prompty.PromptyChatParser, "parse", lambda *args: messages)
    with pytest.raises(ValueError, match="one system message and one user message"):
        chat_approach.prompt_manager.render_prompt(chat_approach.work_answer_prompt, {})


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"past_messages": {}}, "history must be a list"),
        ({"past_messages": ["invalid"]}, "history must contain text messages"),
        ({"past_messages": [{"role": "user", "content": None}]}, "history must contain text messages"),
        ({"past_messages": [{"role": "system", "content": "Invalid."}]}, "Unsupported prompt history role"),
        ({"text_sources": "invalid"}, "sources must be a list of text"),
        ({"text_sources": [None]}, "sources must be a list of text"),
    ],
)
def test_prompt_rejects_invalid_context(chat_approach, data, error):
    with pytest.raises(ValueError, match=error):
        chat_approach.prompt_manager.render_prompt(chat_approach.work_answer_prompt, data)


@pytest.mark.parametrize("path_kind", ["absolute", "relative"])
def test_prompt_load_rejects_paths_outside_bundle(chat_approach, monkeypatch, tmp_path, path_kind):
    root = tmp_path / "prompts"
    root.mkdir()
    external = tmp_path / "external.prompty"
    external.write_text("---\nname: External\n---\nuser:\nExample.", encoding="utf-8")
    monkeypatch.setattr(chat_approach.prompt_manager, "PROMPTS_DIRECTORY", root)
    path = str(external) if path_kind == "absolute" else "../external.prompty"

    with pytest.raises(ValueError, match="bundled prompt directory"):
        chat_approach.prompt_manager.load_prompt(path)


@pytest.mark.parametrize(
    ("model", "expected_limit"),
    [("gpt-4.1-mini", 1024), ("o1", 8192), ("o3", 8192), ("gpt-5", 8192)],
)
def test_response_token_limit(chat_approach, model, expected_limit):
    assert chat_approach.get_response_token_limit(model, 1024) == expected_limit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "overrides", "reasoning_effort", "expected_effort"),
    [
        ("gpt-5", {}, None, "medium"),
        ("gpt-5", {"reasoning_effort": "minimal"}, None, "minimal"),
        ("o3", {"reasoning_effort": "high"}, "low", "low"),
    ],
)
async def test_create_chat_completion_reasoning_effort(
    chat_approach, model, overrides, reasoning_effort, expected_effort
):
    chat_approach.openai_client = AsyncMock()
    chat_approach.reasoning_effort = "medium"

    await chat_approach.create_chat_completion(
        chatgpt_deployment="chat",
        chatgpt_model=model,
        messages=[{"role": "user", "content": "Hello"}],
        overrides=overrides,
        response_token_limit=chat_approach.get_response_token_limit(model, 1024),
        reasoning_effort=reasoning_effort,
    )

    completion = chat_approach.openai_client.chat.completions.create
    completion.assert_awaited_once()
    params = completion.await_args.kwargs
    assert params["model"] == "chat"
    assert params["reasoning_effort"] == expected_effort
    assert params["max_completion_tokens"] == 8192
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_extract_followup_questions(chat_approach):
    content = "Here is answer to your question.<<What is the dress code?>>"
    pre_content, followup_questions = chat_approach.extract_followup_questions(content)
    assert pre_content == "Here is answer to your question."
    assert followup_questions == ["What is the dress code?"]


def test_extract_followup_questions_three(chat_approach):
    content = """Here is answer to your question.

<<What are some examples of successful product launches they should have experience with?>>
<<Are there any specific technical skills or certifications required for the role?>>
<<Is there a preference for candidates with experience in a specific industry or sector?>>"""
    pre_content, followup_questions = chat_approach.extract_followup_questions(content)
    assert pre_content == "Here is answer to your question.\n\n"
    assert followup_questions == [
        "What are some examples of successful product launches they should have experience with?",
        "Are there any specific technical skills or certifications required for the role?",
        "Is there a preference for candidates with experience in a specific industry or sector?",
    ]


def test_extract_followup_questions_no_followup(chat_approach):
    content = "Here is answer to your question."
    pre_content, followup_questions = chat_approach.extract_followup_questions(content)
    assert pre_content == "Here is answer to your question."
    assert followup_questions == []


def test_extract_followup_questions_no_pre_content(chat_approach):
    content = "<<What is the dress code?>>"
    pre_content, followup_questions = chat_approach.extract_followup_questions(content)
    assert pre_content == ""
    assert followup_questions == ["What is the dress code?"]


@pytest.mark.asyncio
async def test_run_with_streaming_handles_non_stream_response(chat_approach, monkeypatch):
    extra_info = ExtraInfo(
        data_points=DataPoints(text=[], images=[], citations=[]),
        thoughts=[ThoughtStep("Final", None, props={})],
    )

    async def fake_completion():
        payload = {
            "id": "chatcmpl-stream",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4.1-mini",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Answer text<<Follow up?>>"},
                }
            ],
            "usage": {"completion_tokens": 1, "prompt_tokens": 1, "total_tokens": 2},
        }
        return ChatCompletion.model_validate(payload, strict=False)

    async def fake_run_until_final_call(messages, overrides, auth_claims, should_stream, admin_settings={}):
        assert should_stream is True
        return extra_info, fake_completion()

    monkeypatch.setattr(chat_approach, "run_until_final_call", fake_run_until_final_call)

    events = []
    async for event in chat_approach.run_with_streaming(
        messages=[{"role": "user", "content": "Hello"}],
        overrides={"suggest_followup_questions": True},
        auth_claims={},
        session_state="state",
    ):
        events.append(event)

    assert events[0]["context"] is extra_info
    assert events[1]["delta"]["content"] == "Answer text"
    assert events[2]["context"] is extra_info
    assert events[3]["context"]["followup_questions"] == ["Follow up?"]


def test_is_conversational_query():
    assert PersonaChatApproach.is_conversational_query("hi") is True
    assert PersonaChatApproach.is_conversational_query("hello!") is True
    assert PersonaChatApproach.is_conversational_query("What is your tech stack?") is False


def test_is_small_talk():
    assert PersonaChatApproach.is_small_talk("tell me a joke") is True
    assert PersonaChatApproach.is_small_talk("What technologies do you use?") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("should_stream", [False, True])
@pytest.mark.parametrize(
    ("question", "fetched_content"),
    [
        ("https://example.com/job", "Role requires Python and accessible interfaces."),
        ("Am I a fit? https://example.com/job", "Role requires Python and accessible interfaces."),
        ("Am I a fit? https://example.com/job", None),
    ],
)
async def test_chat_incorporates_fetched_job_without_mutating_history(
    chat_approach, monkeypatch, should_stream, question, fetched_content
):
    fetch = AsyncMock(return_value=fetched_content)
    monkeypatch.setattr("core.urlfetcher.extract_url_content", fetch)
    chat_approach.openai_client = AsyncMock()
    messages = [
        {"role": "user", "content": "Tell me about your work"},
        {"role": "assistant", "content": "I build AI applications."},
        {"role": "user", "content": question},
    ]
    original_messages = deepcopy(messages)

    extra_info, completion = await chat_approach.run_until_final_call(
        messages, overrides={}, auth_claims={}, should_stream=should_stream
    )
    await completion

    fetch.assert_awaited_once_with("https://example.com/job")
    assert messages == original_messages
    parameters = chat_approach.openai_client.chat.completions.create.await_args.kwargs
    assert parameters.get("stream", False) is should_stream
    rendered_messages = parameters["messages"]
    assert rendered_messages[-1]["role"] == "user"
    assert any(message["content"] == "I build AI applications." for message in rendered_messages)
    fetch_steps = [step for step in extra_info.thoughts if step.title == "Fetched job description from URL"]
    if fetched_content is None:
        assert question in rendered_messages[-1]["content"]
        assert fetch_steps == []
    else:
        assert fetched_content in rendered_messages[-1]["content"]
        assert "--- Job Description ---" in rendered_messages[-1]["content"]
        assert len(fetch_steps) == 1
        assert fetch_steps[0].description == "https://example.com/job"


@pytest.mark.asyncio
async def test_chat_blocks_private_url_before_http_request(chat_approach, monkeypatch):
    http_session = Mock(side_effect=AssertionError("Private URL must not open a connection"))
    monkeypatch.setattr("core.urlfetcher.aiohttp.ClientSession", http_session)
    chat_approach.openai_client = AsyncMock()
    question = "Read http://127.0.0.1/internal"

    extra_info, completion = await chat_approach.run_until_final_call(
        [{"role": "user", "content": question}], overrides={}, auth_claims={}
    )
    await completion

    http_session.assert_not_called()
    assert all(step.title != "Fetched job description from URL" for step in extra_info.thoughts)
    rendered = chat_approach.openai_client.chat.completions.create.await_args.kwargs["messages"]
    assert question in rendered[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Tell me about yourself",
        "What's your favorite project?",
        "What experience do you have building email systems?",
        "Can I call you to discuss your AI projects?",
        "hi",
    ],
)
async def test_tone_classification_never_removes_grounding(chat_approach, question):
    chat_approach.openai_client = AsyncMock()
    extra_info, completion = await chat_approach.run_until_final_call([{"role": "user", "content": question}], {}, {})
    await completion
    messages = chat_approach.openai_client.chat.completions.create.await_args.kwargs["messages"]
    assert chat_approach.knowledge_sources
    assert extra_info.data_points.text == chat_approach.knowledge_sources
    assert all(source in messages[-1]["content"] for source in chat_approach.knowledge_sources)


def test_prompt_budget_preserves_exact_boundary_and_complete_recent_turns(chat_approach):
    messages = [
        {"role": "system", "content": "Required instructions."},
        {"role": "user", "content": "Old question."},
        {"role": "assistant", "content": "Old answer."},
        {"role": "user", "content": "Recent question."},
        {"role": "assistant", "content": "Recent answer."},
        {"role": "user", "content": "Latest question and required knowledge."},
    ]
    original = deepcopy(messages)
    response_limit = 1024
    exact_tokens = sum(chat_approach.count_message_tokens(message) for message in messages) + 3
    chat_approach.chatgpt_context_window = exact_tokens + response_limit + chat_approach.TOKEN_BUDGET_MARGIN
    fitted, removed = chat_approach.fit_messages_to_context(messages, response_limit)
    assert fitted == messages
    assert removed == 0

    chat_approach.chatgpt_context_window -= 1
    fitted, removed = chat_approach.fit_messages_to_context(messages, response_limit)
    assert fitted == [messages[0], *messages[3:]]
    assert removed == 2
    assert messages == original
    assert sum(chat_approach.count_message_tokens(message) for message in fitted) + 3 <= (
        chat_approach.chatgpt_context_window - response_limit - chat_approach.TOKEN_BUDGET_MARGIN
    )


def test_prompt_budget_rejects_oversized_mandatory_context(chat_approach):
    messages = [
        {"role": "system", "content": "Required instructions."},
        {"role": "user", "content": "Required knowledge and latest question."},
    ]
    mandatory_tokens = sum(chat_approach.count_message_tokens(message) for message in messages) + 3
    chat_approach.chatgpt_context_window = mandatory_tokens + 1024 + chat_approach.TOKEN_BUDGET_MARGIN - 1
    with pytest.raises(PromptTooLargeError, match="required knowledge and latest question"):
        chat_approach.fit_messages_to_context(messages, 1024)


def test_prompt_token_count_allows_literal_special_tokens(chat_approach):
    assert chat_approach.count_message_tokens({"role": "user", "content": "<|endoftext|>"}) > 0


def test_prompt_token_count_rejects_non_text_content(chat_approach):
    with pytest.raises(ValueError, match="text messages"):
        chat_approach.count_message_tokens({"role": "user", "content": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("should_stream", [False, True])
async def test_context_trimming_is_reported_without_changing_saved_history(chat_approach, should_stream):
    chat_approach.chatgpt_context_window = 4096
    chat_approach.openai_client = AsyncMock()
    history = [
        {"role": "user", "content": "Old context. " * 4000},
        {"role": "assistant", "content": "Old response."},
        {"role": "user", "content": "What do you work on?"},
    ]
    original = deepcopy(history)
    extra_info, completion = await chat_approach.run_until_final_call(history, {}, {}, should_stream=should_stream)
    await completion
    messages = chat_approach.openai_client.chat.completions.create.await_args.kwargs["messages"]
    assert extra_info.history_truncated is True
    assert [message["role"] for message in messages] == ["system", "user"]
    assert all(source in messages[-1]["content"] for source in chat_approach.knowledge_sources)
    assert history == original
