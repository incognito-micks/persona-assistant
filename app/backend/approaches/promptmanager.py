import pathlib
from typing import Any

import prompty
from openai.types.chat import ChatCompletionMessageParam


class PromptManager:

    def load_prompt(self, path: str) -> prompty.Agent:
        raise NotImplementedError

    def render_prompt(self, prompt: prompty.Agent, data: dict[str, Any]) -> list[ChatCompletionMessageParam]:
        raise NotImplementedError


class PromptyManager(PromptManager):

    PROMPTS_DIRECTORY = pathlib.Path(__file__).parent / "prompts"

    def load_prompt(self, path: str) -> prompty.Agent:
        root = self.PROMPTS_DIRECTORY.resolve()
        prompt_path = (root / path).resolve()
        if not prompt_path.is_relative_to(root):
            raise ValueError("Prompt must be inside the bundled prompt directory")
        return prompty.load(prompt_path, allowed_file_roots=[root])

    def render_prompt(self, prompt: prompty.Agent, data: dict[str, Any]) -> list[ChatCompletionMessageParam]:
        inputs = dict(data)
        past_messages = inputs.get("past_messages", [])
        if not isinstance(past_messages, list):
            raise ValueError("Prompt history must be a list of text messages")
        history: list[ChatCompletionMessageParam] = []
        for message in past_messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ValueError("Prompt history must contain text messages")
            if message.get("role") == "user":
                history.append({"role": "user", "content": message["content"]})
            elif message.get("role") == "assistant":
                history.append({"role": "assistant", "content": message["content"]})
            else:
                raise ValueError("Unsupported prompt history role")
        inputs.pop("past_messages", None)

        text_sources = inputs.get("text_sources", [])
        if not isinstance(text_sources, list) or any(not isinstance(source, str) for source in text_sources):
            raise ValueError("Prompt sources must be a list of text")
        inputs["text_sources"] = "\n\n".join(text_sources)
        messages: list[ChatCompletionMessageParam] = []
        renderer = prompty.Jinja2Renderer()
        # Parse only trusted template structure, never interpolated visitor text.
        for message in prompty.PromptyChatParser().parse(prompt, prompt.instructions or ""):
            content_parts: list[str] = []
            for part in message.parts:
                if not isinstance(part, prompty.TextPart):
                    raise ValueError("Text prompts cannot contain non-text content")
                content_parts.append(part.value)
            content = renderer.render(prompt, "".join(content_parts), inputs).strip("\n")
            if message.role == "system":
                messages.append({"role": "system", "content": content})
            elif message.role == "user":
                messages.append({"role": "user", "content": content})
            else:
                raise ValueError("Unsupported prompt message role")
        if len(messages) != 2 or messages[0]["role"] != "system" or messages[1]["role"] != "user":
            raise ValueError("Prompt must render one system message and one user message")
        return [messages[0], *history, messages[1]]
