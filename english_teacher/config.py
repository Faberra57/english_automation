from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
INPUT_MODES = {"write_only", "audio_only", "both"}


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "oui"}:
        return True
    if normalized in {"0", "false", "no", "off", "non"}:
        return False
    raise ValueError(f"Valeur booléenne invalide: {value!r}")


def env_int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier") from exc
    if value < minimum:
        raise ValueError(f"{name} doit être >= {minimum}")
    return value


def env_float(env: Mapping[str, str], name: str, default: float, minimum: float = 0) -> float:
    try:
        value = float(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} doit être un nombre") from exc
    if value < minimum:
        raise ValueError(f"{name} doit être >= {minimum}")
    return value


def csv_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_clock(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise ValueError(f"Heure invalide {value!r}; format attendu HH:MM")
    return int(match.group(1)), int(match.group(2))


def parse_days(value: str) -> tuple[int, ...]:
    values = csv_values(value.lower())
    if not values or values == ("all",):
        return tuple(range(7))
    unknown = [item for item in values if item not in WEEKDAYS]
    if unknown:
        raise ValueError(f"Jours inconnus: {', '.join(unknown)}")
    return tuple(dict.fromkeys(WEEKDAYS[item] for item in values))


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    telegram_chat_id: int
    telegram_base_url: str
    telegram_base_file_url: str
    telegram_max_audio_mb: int
    telegram_ack_message: str
    input_mode: str
    timezone: ZoneInfo
    morning_enabled: bool
    morning_clock: tuple[int, int]
    morning_days: tuple[int, ...]
    evening_enabled: bool
    evening_clock: tuple[int, int]
    evening_days: tuple[int, ...]
    anki_retry_enabled: bool
    anki_retry_clock: tuple[int, int]
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_temperature: float
    deepseek_max_tokens: int
    deepseek_timeout: float
    deepseek_max_retries: int
    deepseek_reasoning_effort: str
    deepseek_thinking_enabled: bool
    groq_api_key: str
    groq_base_url: str
    groq_model: str
    groq_language: str
    groq_prompt: str
    groq_timeout: float
    groq_max_retries: int
    learner_name: str
    learner_level: str
    explanation_language: str
    target_language: str
    learner_interests: str
    daily_activity_minutes: int
    max_errors: int
    daily_card_limit: int
    correction_style: str
    rag_top_k: int
    rag_candidate_limit: int
    rag_history_days: int
    rag_max_context_chars: int
    rag_half_life_days: float
    data_dir: Path
    database_path: Path
    audio_dir: Path
    log_level: str
    audio_retention_days: int
    anki_enabled: bool
    ankiconnect_url: str
    ankiconnect_api_key: str
    anki_timeout: float
    anki_deck: str
    anki_model: str
    anki_front_field: str
    anki_back_field: str
    anki_tags: tuple[str, ...]
    anki_create_deck: bool
    anki_sync_after_push: bool
    anki_duplicate_scope: str

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> "Settings":
        env = source or os.environ
        input_mode = env.get("INPUT_MODE", "both").strip().lower()
        if input_mode not in INPUT_MODES:
            raise ValueError(
                f"INPUT_MODE invalide: {input_mode!r}; valeurs attendues: write_only, audio_only ou both"
            )
        try:
            timezone = ZoneInfo(env.get("TZ", "Europe/Paris"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"TZ inconnu: {env.get('TZ')}") from exc
        try:
            allowed = frozenset(int(item) for item in csv_values(env.get("TELEGRAM_ALLOWED_USER_IDS", "")))
            chat_id = int(env.get("TELEGRAM_CHAT_ID", "0"))
        except ValueError as exc:
            raise ValueError("Les IDs Telegram doivent être des entiers") from exc
        data_dir = Path(env.get("DATA_DIR", "/app/data")).expanduser()
        return cls(
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=allowed,
            telegram_chat_id=chat_id,
            telegram_base_url=env.get("TELEGRAM_BASE_URL", "").strip().rstrip("/"),
            telegram_base_file_url=env.get("TELEGRAM_BASE_FILE_URL", "").strip().rstrip("/"),
            telegram_max_audio_mb=env_int(env, "TELEGRAM_MAX_AUDIO_MB", 19, 1),
            telegram_ack_message=env.get("TELEGRAM_ACK_MESSAGE", "Got it — I’m preparing your correction…"),
            input_mode=input_mode,
            timezone=timezone,
            morning_enabled=env_bool(env.get("MORNING_ENABLED"), True),
            morning_clock=parse_clock(env.get("MORNING_TIME", "07:30")),
            morning_days=parse_days(env.get("MORNING_DAYS", "all")),
            evening_enabled=env_bool(env.get("EVENING_ENABLED"), True),
            evening_clock=parse_clock(env.get("EVENING_TIME", "20:30")),
            evening_days=parse_days(env.get("EVENING_DAYS", "all")),
            anki_retry_enabled=env_bool(env.get("ANKI_RETRY_ENABLED"), True),
            anki_retry_clock=parse_clock(env.get("ANKI_RETRY_TIME", "23:00")),
            deepseek_api_key=env.get("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            deepseek_model=env.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_temperature=env_float(env, "DEEPSEEK_TEMPERATURE", 0.2),
            deepseek_max_tokens=env_int(env, "DEEPSEEK_MAX_TOKENS", 2500, 128),
            deepseek_timeout=env_float(env, "DEEPSEEK_TIMEOUT_SECONDS", 120, 1),
            deepseek_max_retries=env_int(env, "DEEPSEEK_MAX_RETRIES", 3, 1),
            deepseek_reasoning_effort=env.get("DEEPSEEK_REASONING_EFFORT", "").strip(),
            deepseek_thinking_enabled=env_bool(env.get("DEEPSEEK_THINKING_ENABLED"), False),
            groq_api_key=env.get("GROQ_API_KEY", "").strip(),
            groq_base_url=env.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            groq_model=env.get("GROQ_WHISPER_MODEL", "whisper-large-v3"),
            groq_language=env.get("GROQ_LANGUAGE", "en"),
            groq_prompt=env.get("GROQ_TRANSCRIPTION_PROMPT", "English learner speaking practice. Preserve mistakes exactly."),
            groq_timeout=env_float(env, "GROQ_TIMEOUT_SECONDS", 180, 1),
            groq_max_retries=env_int(env, "GROQ_MAX_RETRIES", 3, 1),
            learner_name=env.get("LEARNER_NAME", "Learner"),
            learner_level=env.get("LEARNER_LEVEL", "B1"),
            explanation_language=env.get("EXPLANATION_LANGUAGE", "English"),
            target_language=env.get("TARGET_LANGUAGE", "English"),
            learner_interests=env.get("LEARNER_INTERESTS", "vie quotidienne"),
            daily_activity_minutes=env_int(env, "DAILY_ACTIVITY_MINUTES", 5, 1),
            max_errors=env_int(env, "MAX_ERRORS_PER_SUBMISSION", 25, 1),
            daily_card_limit=env_int(env, "DAILY_CARD_LIMIT", 5, 1),
            correction_style=env.get("CORRECTION_STYLE", "encouraging, direct, and concise"),
            rag_top_k=env_int(env, "RAG_TOP_K", 12, 1),
            rag_candidate_limit=env_int(env, "RAG_CANDIDATE_LIMIT", 300, 1),
            rag_history_days=env_int(env, "RAG_HISTORY_DAYS", 180, 1),
            rag_max_context_chars=env_int(env, "RAG_MAX_CONTEXT_CHARS", 10000, 500),
            rag_half_life_days=env_float(env, "RAG_RECENCY_HALF_LIFE_DAYS", 30, 1),
            data_dir=data_dir,
            database_path=Path(env.get("DATABASE_PATH", str(data_dir / "english_teacher.sqlite3"))).expanduser(),
            audio_dir=Path(env.get("AUDIO_DIR", str(data_dir / "audio"))).expanduser(),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            audio_retention_days=env_int(env, "AUDIO_RETENTION_DAYS", 0, 0),
            anki_enabled=env_bool(env.get("ANKI_ENABLED"), True),
            ankiconnect_url=env.get("ANKICONNECT_URL", "http://127.0.0.1:8765").rstrip("/"),
            ankiconnect_api_key=env.get("ANKICONNECT_API_KEY", "").strip(),
            anki_timeout=env_float(env, "ANKI_TIMEOUT_SECONDS", 20, 1),
            anki_deck=env.get("ANKI_DECK", "English::AI Teacher"),
            anki_model=env.get("ANKI_MODEL", "Basic"),
            anki_front_field=env.get("ANKI_FRONT_FIELD", "Front"),
            anki_back_field=env.get("ANKI_BACK_FIELD", "Back"),
            anki_tags=csv_values(env.get("ANKI_TAGS", "english_ai,auto_generated")),
            anki_create_deck=env_bool(env.get("ANKI_CREATE_DECK"), True),
            anki_sync_after_push=env_bool(env.get("ANKI_SYNC_AFTER_PUSH"), True),
            anki_duplicate_scope=env.get("ANKI_DUPLICATE_SCOPE", "deck"),
        )

    def validate(self, require_secrets: bool = True) -> None:
        errors: list[str] = []
        if require_secrets and (not self.telegram_bot_token or "replace_me" in self.telegram_bot_token):
            errors.append("TELEGRAM_BOT_TOKEN manquant")
        if not self.allowed_user_ids:
            errors.append("TELEGRAM_ALLOWED_USER_IDS manquant (sécurité fail-closed)")
        if not self.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID manquant")
        if require_secrets and (not self.deepseek_api_key or self.deepseek_api_key == "replace_me"):
            errors.append("DEEPSEEK_API_KEY manquant")
        if require_secrets and (not self.groq_api_key or self.groq_api_key == "replace_me"):
            errors.append("GROQ_API_KEY manquant")
        if self.telegram_chat_id not in self.allowed_user_ids:
            errors.append("TELEGRAM_CHAT_ID doit aussi être présent dans TELEGRAM_ALLOWED_USER_IDS")
        if errors:
            raise ValueError("; ".join(errors))

    def local_date(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def scheduled_time(self, clock: tuple[int, int]) -> time:
        return time(clock[0], clock[1], tzinfo=self.timezone)
