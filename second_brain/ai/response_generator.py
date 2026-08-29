"""Render safe action results into user-facing text."""

from __future__ import annotations

from second_brain.ai.action_dispatcher import ActionResult


class ResponseGenerator:
    """Deterministic Phase 2 response renderer."""

    def generate(self, result: ActionResult) -> str:
        if not result.ok:
            return _localized(result.language, result.message)

        if result.intent in {"list_entities", "list_files", "search_entities"}:
            entities = result.data.get("entities", [])
            if not entities:
                return _localized(result.language, "No entities found.")
            noun = "files" if result.intent == "list_files" else "entities"
            if result.intent == "search_entities":
                noun = f"matches for '{result.data.get('query', '')}'"
            lines = [_localized(result.language, f"Found {len(entities)} {noun}:")]
            lines.extend(f"- {item['id']} ({item['type']}): {item['name']}" for item in entities)
            return "\n".join(lines)

        if result.intent == "list_relationships":
            relationships = result.data.get("relationships", [])
            if not relationships:
                return _localized(result.language, "No relationships found.")
            lines = [_localized(result.language, f"Found {len(relationships)} relationships:")]
            lines.extend(
                f"- {item['source_id']} -> {item['type']} -> {item['target_id']}"
                for item in relationships
            )
            return "\n".join(lines)

        if result.intent in {"get_important_tasks", "get_high_priority_tasks"}:
            tasks = result.data.get("tasks", [])
            if not tasks:
                return _localized(result.language, "No tasks matched.")
            label = "Important tasks" if result.intent == "get_important_tasks" else "High priority tasks"
            lines = [_localized(result.language, f"{label}:")]
            lines.extend(f"- {task['id']}: {task['name']}" for task in tasks)
            return "\n".join(lines)

        if result.intent == "explain_high_priority":
            explanation = result.data["explanation"]
            lines = [_localized(result.language, explanation["title"])]
            lines.extend(f"{index}. {step}" for index, step in enumerate(explanation["steps"], start=1))
            return "\n".join(lines)

        return _localized(result.language, result.message)


def _localized(language: str, english_text: str) -> str:
    if language in {"my", "mixed"}:
        translations = {
            "No entities found.": "Entities မတွေ့ပါ။",
            "No relationships found.": "Relationships မတွေ့ပါ။",
            "No tasks matched.": "ကိုက်ညီတဲ့ tasks မတွေ့ပါ။",
            "I could not confidently map that request to a safe Phase 2 action.": (
                "ဒီ request ကို safe Phase 2 action အဖြစ် မသေချာလို့ မလုပ်ဆောင်ပါ။"
            ),
            "This action requires confirmation and was not executed.": (
                "ဒီ action က confirmation လိုအပ်လို့ မလုပ်ဆောင်ပါ။"
            ),
        }
        return translations.get(english_text, english_text)
    return english_text
