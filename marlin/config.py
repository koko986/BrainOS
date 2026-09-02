"""Configuration for the fully local MARLIN V2 runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class MarlinSettings:
    database_path: Path = PROJECT_ROOT / "data" / "database" / "second_brain.db"
    prolog_dir: Path = PROJECT_ROOT / "prolog"
    host: str = "127.0.0.1"
    port: int = 8765
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b-instruct"
    ollama_timeout_seconds: float = 45.0
    ollama_keep_alive: str = "30m"
    ollama_context: int = 2048
    ollama_max_tokens: int = 160
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    microphone_device: str = ""
    wake_word_enabled: bool = True
    piper_voice: str = "en_GB-alan-medium"
    piper_data_dir: Path = PROJECT_ROOT / "models" / "piper"
    vosk_model_path: Path = PROJECT_ROOT / "models" / "vosk-model-small-en-us-0.15"
    auto_index_c_drive: bool = True
    index_batch_size: int = 5000
    weather_enabled: bool = True
    weather_latitude: float = 16.8409
    weather_longitude: float = 96.1735
    launch_on_login: bool = False
    voice_output: bool = True

    @classmethod
    def from_env(cls) -> "MarlinSettings":
        _load_env()
        defaults = cls()
        return cls(
            database_path=Path(os.getenv("MARLIN_DB_PATH", str(defaults.database_path))).expanduser(),
            prolog_dir=Path(os.getenv("MARLIN_PROLOG_DIR", str(defaults.prolog_dir))).expanduser(),
            host=os.getenv("MARLIN_HOST", defaults.host),
            port=int(os.getenv("MARLIN_PORT", str(defaults.port))),
            ollama_url=os.getenv("MARLIN_OLLAMA_URL", defaults.ollama_url),
            ollama_model=os.getenv("MARLIN_OLLAMA_MODEL", defaults.ollama_model),
            ollama_timeout_seconds=float(os.getenv("MARLIN_OLLAMA_TIMEOUT", str(defaults.ollama_timeout_seconds))),
            ollama_keep_alive=os.getenv("MARLIN_OLLAMA_KEEP_ALIVE", defaults.ollama_keep_alive),
            ollama_context=int(os.getenv("MARLIN_OLLAMA_CONTEXT", str(defaults.ollama_context))),
            ollama_max_tokens=int(os.getenv("MARLIN_OLLAMA_MAX_TOKENS", str(defaults.ollama_max_tokens))),
            whisper_model=os.getenv("MARLIN_WHISPER_MODEL", defaults.whisper_model),
            whisper_device=os.getenv("MARLIN_WHISPER_DEVICE", defaults.whisper_device),
            whisper_compute_type=os.getenv("MARLIN_WHISPER_COMPUTE", defaults.whisper_compute_type),
            microphone_device=os.getenv("MARLIN_MICROPHONE_DEVICE", defaults.microphone_device),
            wake_word_enabled=_bool("MARLIN_WAKE_WORD_ENABLED", defaults.wake_word_enabled),
            piper_voice=os.getenv("MARLIN_PIPER_VOICE", defaults.piper_voice),
            piper_data_dir=Path(os.getenv("MARLIN_PIPER_DATA_DIR", str(defaults.piper_data_dir))).expanduser(),
            vosk_model_path=Path(os.getenv("MARLIN_VOSK_MODEL_PATH", str(defaults.vosk_model_path))).expanduser(),
            auto_index_c_drive=_bool("MARLIN_AUTO_INDEX_C", defaults.auto_index_c_drive),
            index_batch_size=int(os.getenv("MARLIN_INDEX_BATCH_SIZE", str(defaults.index_batch_size))),
            weather_enabled=_bool("MARLIN_WEATHER_ENABLED", defaults.weather_enabled),
            weather_latitude=float(os.getenv("MARLIN_WEATHER_LATITUDE", str(defaults.weather_latitude))),
            weather_longitude=float(os.getenv("MARLIN_WEATHER_LONGITUDE", str(defaults.weather_longitude))),
            launch_on_login=_bool("MARLIN_LAUNCH_ON_LOGIN", defaults.launch_on_login),
            voice_output=_bool("MARLIN_VOICE_OUTPUT", defaults.voice_output),
        )
