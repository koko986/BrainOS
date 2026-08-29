"""Convert natural language into safe structured intents."""

from __future__ import annotations

from pydantic import ValidationError

from second_brain.ai.intent_schema import LanguageCode, StructuredIntent, unknown_intent
from second_brain.ai.llm import ChatMessage, LLMClient


INTENT_SYSTEM_PROMPT = """You are the intent parser for Second Brain AI.
Return only JSON that matches the provided schema.

Safety rules:
- Never output shell commands, SQL, Python code, Prolog queries, or executable text.
- Choose only one supported intent.
- Map English, Burmese, and mixed-language input to the same language-independent intents.
- Use language "my" for Burmese, "mixed" for mixed Burmese/English, "en" for English.
- Use "unknown" when the user asks for unsupported, unsafe, or ambiguous actions.
- For explain_high_priority, include parameters.task_id if the user provided an exact task ID.
- Set requires_confirmation=true for any request that would mutate data.
- Set requires_confirmation=true for every computer action, including opening camera, files, folders, or apps.
- For open_folder/open_file/index_folder, include parameters.path only when the user provides a specific path or named folder.
- For open_app, include parameters.app_name.
- For search_files, include parameters.query.
- Use unknown for delete, move, rename, shell, terminal, command execution, unrestricted screen control, or whole-drive scans.

Supported intents:
- list_entities
- list_files
- list_relationships
- search_entities
- search_files
- get_important_tasks
- get_high_priority_tasks
- explain_high_priority
- seed_demo
- open_camera
- close_camera
- open_folder
- open_file
- open_app
- index_folder
- unknown
"""


class IntentParser:
    """LLM-backed structured intent parser with safe fallback."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def parse(self, user_text: str) -> StructuredIntent:
        messages = [
            ChatMessage(role="system", content=INTENT_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_text),
        ]
        raw_response = self.llm_client.structured_chat(
            messages,
            schema=StructuredIntent.model_json_schema(),
            temperature=0.0,
        )
        try:
            return StructuredIntent.model_validate_json(raw_response)
        except ValidationError:
            return unknown_intent(_detect_language(user_text))


def _detect_language(text: str) -> LanguageCode:
    has_burmese = any("\u1000" <= char <= "\u109f" for char in text)
    has_latin = any("a" <= char.lower() <= "z" for char in text)
    if has_burmese and has_latin:
        return "mixed"
    if has_burmese:
        return "my"
    if has_latin:
        return "en"
    return "unknown"
