"""Runtime configuration for Second Brain AI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_local_env_file() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    """Small settings object for the Phase 1 prototype."""

    database_path: Path = PROJECT_ROOT / "data" / "database" / "second_brain.db"
    prolog_dir: Path = PROJECT_ROOT / "prolog"
    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_referer: str = "http://127.0.0.1:8765"
    openrouter_title: str = "MARLIN BrainOS"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"
    tts_provider: str = "windows"
    voice_mode: str = "human"
    gemini_tts_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_tts_model: str = "gemini-3.1-flash-tts-preview"
    gemini_tts_voice: str = "Gacrux"
    computer_confirmation: str = "always"
    allowed_apps: str = "notepad,calculator,explorer,vscode,chrome"
    cockpit_host: str = "127.0.0.1"
    cockpit_port: int = 8765
    conversation_provider: str = "opencode"
    opencode_command: str = "opencode"
    opencode_model: str = "opencode/muse-spark-1.2-contributor-free"
    opencode_timeout_seconds: float = 6.0
    opencode_use_server: bool = True
    opencode_server_host: str = "127.0.0.1"
    opencode_server_port: int = 4096
    opencode_continue_session: bool = True
    fast_local_conversation: bool = True
    terminal_voice_output: bool = True
    terminal_voice_input_model_path: str = ""
    terminal_voice_input_seconds: float = 6.0
    stt_provider: str = "groq"
    groq_stt_model: str = "whisper-large-v3-turbo"
    stt_language: str = ""
    vosk_model_path: str = "models/vosk-model-small-en-us-0.15"
    wake_word_enabled: bool = True
    wake_words: str = "marlin,hey marlin,hey marlon,hey merlin"
    voice_max_seconds: float = 12.0
    voice_silence_seconds: float = 0.8
    voice_start_timeout: float = 8.0
    file_access: str = "full"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env_file()
        database_path = Path(
            os.getenv("SECOND_BRAIN_DB_PATH", str(cls.database_path))
        ).expanduser()
        prolog_dir = Path(os.getenv("SECOND_BRAIN_PROLOG_DIR", str(cls.prolog_dir))).expanduser()
        return cls(
            database_path=database_path,
            prolog_dir=prolog_dir,
            llm_provider=os.getenv("SECOND_BRAIN_LLM_PROVIDER", cls.llm_provider),
            ollama_url=os.getenv("SECOND_BRAIN_OLLAMA_URL", cls.ollama_url),
            ollama_model=os.getenv("SECOND_BRAIN_OLLAMA_MODEL", cls.ollama_model),
            openai_base_url=os.getenv("SECOND_BRAIN_OPENAI_BASE_URL", cls.openai_base_url),
            openai_model=os.getenv("SECOND_BRAIN_OPENAI_MODEL", cls.openai_model),
            openrouter_base_url=os.getenv(
                "SECOND_BRAIN_OPENROUTER_BASE_URL",
                cls.openrouter_base_url,
            ),
            openrouter_model=os.getenv("SECOND_BRAIN_OPENROUTER_MODEL", cls.openrouter_model),
            openrouter_referer=os.getenv(
                "SECOND_BRAIN_OPENROUTER_REFERER",
                cls.openrouter_referer,
            ),
            openrouter_title=os.getenv("SECOND_BRAIN_OPENROUTER_TITLE", cls.openrouter_title),
            groq_base_url=os.getenv("SECOND_BRAIN_GROQ_BASE_URL", cls.groq_base_url),
            groq_model=os.getenv("SECOND_BRAIN_GROQ_MODEL", cls.groq_model),
            tts_provider=os.getenv("SECOND_BRAIN_TTS_PROVIDER", cls.tts_provider),
            voice_mode=os.getenv("SECOND_BRAIN_VOICE_MODE", cls.voice_mode),
            gemini_tts_base_url=os.getenv(
                "SECOND_BRAIN_GEMINI_TTS_BASE_URL",
                cls.gemini_tts_base_url,
            ),
            gemini_tts_model=os.getenv("SECOND_BRAIN_GEMINI_TTS_MODEL", cls.gemini_tts_model),
            gemini_tts_voice=os.getenv("SECOND_BRAIN_GEMINI_TTS_VOICE", cls.gemini_tts_voice),
            computer_confirmation=os.getenv(
                "SECOND_BRAIN_COMPUTER_CONFIRMATION",
                cls.computer_confirmation,
            ),
            allowed_apps=os.getenv("SECOND_BRAIN_ALLOWED_APPS", cls.allowed_apps),
            cockpit_host=os.getenv("SECOND_BRAIN_COCKPIT_HOST", cls.cockpit_host),
            cockpit_port=int(os.getenv("SECOND_BRAIN_COCKPIT_PORT", str(cls.cockpit_port))),
            conversation_provider=os.getenv(
                "SECOND_BRAIN_CONVERSATION_PROVIDER",
                cls.conversation_provider,
            ),
            opencode_command=os.getenv("SECOND_BRAIN_OPENCODE_COMMAND", cls.opencode_command),
            opencode_model=os.getenv("SECOND_BRAIN_OPENCODE_MODEL", cls.opencode_model),
            opencode_timeout_seconds=float(
                os.getenv(
                    "SECOND_BRAIN_OPENCODE_TIMEOUT_SECONDS",
                    str(cls.opencode_timeout_seconds),
                )
            ),
            opencode_use_server=os.getenv(
                "SECOND_BRAIN_OPENCODE_USE_SERVER",
                "true" if cls.opencode_use_server else "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            opencode_server_host=os.getenv(
                "SECOND_BRAIN_OPENCODE_SERVER_HOST",
                cls.opencode_server_host,
            ),
            opencode_server_port=int(
                os.getenv(
                    "SECOND_BRAIN_OPENCODE_SERVER_PORT",
                    str(cls.opencode_server_port),
                )
            ),
            opencode_continue_session=os.getenv(
                "SECOND_BRAIN_OPENCODE_CONTINUE_SESSION",
                "true" if cls.opencode_continue_session else "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            fast_local_conversation=os.getenv(
                "SECOND_BRAIN_FAST_LOCAL_CONVERSATION",
                "true" if cls.fast_local_conversation else "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            terminal_voice_output=os.getenv(
                "SECOND_BRAIN_TERMINAL_VOICE_OUTPUT",
                "true" if cls.terminal_voice_output else "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            terminal_voice_input_model_path=os.getenv(
                "SECOND_BRAIN_TERMINAL_VOICE_INPUT_MODEL_PATH",
                cls.terminal_voice_input_model_path,
            ),
            terminal_voice_input_seconds=float(
                os.getenv(
                    "SECOND_BRAIN_TERMINAL_VOICE_INPUT_SECONDS",
                    str(cls.terminal_voice_input_seconds),
                )
            ),
            stt_provider=os.getenv("SECOND_BRAIN_STT_PROVIDER", cls.stt_provider),
            groq_stt_model=os.getenv("SECOND_BRAIN_GROQ_STT_MODEL", cls.groq_stt_model),
            stt_language=os.getenv("SECOND_BRAIN_STT_LANGUAGE", cls.stt_language),
            vosk_model_path=os.getenv("SECOND_BRAIN_VOSK_MODEL_PATH", cls.vosk_model_path),
            wake_word_enabled=os.getenv(
                "SECOND_BRAIN_WAKE_WORD_ENABLED",
                "true" if cls.wake_word_enabled else "false",
            ).lower()
            in {"1", "true", "yes", "on"},
            wake_words=os.getenv("SECOND_BRAIN_WAKE_WORDS", cls.wake_words),
            voice_max_seconds=float(
                os.getenv("SECOND_BRAIN_VOICE_MAX_SECONDS", str(cls.voice_max_seconds))
            ),
            voice_silence_seconds=float(
                os.getenv("SECOND_BRAIN_VOICE_SILENCE_SECONDS", str(cls.voice_silence_seconds))
            ),
            voice_start_timeout=float(
                os.getenv("SECOND_BRAIN_VOICE_START_TIMEOUT", str(cls.voice_start_timeout))
            ),
            file_access=os.getenv("SECOND_BRAIN_FILE_ACCESS", cls.file_access),
        )

