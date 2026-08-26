from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from english_teacher.clients import DeepSeekClient
from english_teacher.config import Settings, parse_clock, parse_days
from english_teacher.database import Database
from english_teacher.utils import chunks


def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    values = {
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "TELEGRAM_ALLOWED_USER_IDS": "42",
        "TELEGRAM_CHAT_ID": "42",
        "DEEPSEEK_API_KEY": "deepseek-test",
        "GROQ_API_KEY": "groq-test",
        "DATA_DIR": str(tmp_path),
        "DATABASE_PATH": str(tmp_path / "teacher.sqlite3"),
        "AUDIO_DIR": str(tmp_path / "audio"),
        "ANKI_ENABLED": "false",
    }
    values.update(overrides)
    return Settings.from_env(values)


def test_settings_parsing_and_security(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, MORNING_TIME="06:45", MORNING_DAYS="mon,wed,sun")
    settings.validate()
    assert settings.morning_clock == (6, 45)
    assert settings.morning_days == (1, 3, 0)
    assert parse_days("all") == tuple(range(7))
    with pytest.raises(ValueError):
        parse_clock("25:10")
    with pytest.raises(ValueError, match="fail-closed"):
        make_settings(tmp_path, TELEGRAM_ALLOWED_USER_IDS="").validate()


@pytest.mark.asyncio
async def test_database_archives_and_retrieves_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    submission_id = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=10,
        kind="text",
        raw_text="I go yesterday.",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-25T08:00:00+00:00",
        local_date="2026-08-25",
    )
    assert submission_id
    duplicate = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=10,
        kind="text",
        raw_text="duplicate",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-25T08:01:00+00:00",
        local_date="2026-08-25",
    )
    assert duplicate is None
    await db.replace_errors(
        submission_id,
        [
            {
                "category": "past tense",
                "original": "I go yesterday",
                "corrected": "I went yesterday",
                "explanation_fr": "Le prétérit de go est went.",
                "practice_tip": "Raconte une action passée.",
                "severity": 4,
                "confidence": 0.99,
            }
        ],
        5,
    )
    context = await db.rag_context("Yesterday I visit a friend")
    assert "I went yesterday" in context
    assert "past tense" in context
    assert (await db.stats())["submissions"] == 1


@pytest.mark.asyncio
async def test_card_queue_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    submission_id = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=11,
        kind="text",
        raw_text="She have a car",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-25T08:00:00+00:00",
        local_date="2026-08-25",
    )
    await db.replace_errors(
        submission_id,
        [{"category": "agreement", "original": "She have", "corrected": "She has", "explanation_fr": "Accord."}],
        5,
    )
    errors = await db.uncarded_errors("2026-08-25", 5)
    assert await db.create_cards([{"error_id": errors[0]["id"], "front": "Accord avec she", "back": "She has a car.", "tags": ["grammar"]}]) == 1
    pending = await db.pending_cards()
    assert len(pending) == 1
    await db.update_card(pending[0]["id"], status="pushed", note_id=123)
    assert (await db.stats())["cards_pushed"] == 1


@pytest.mark.asyncio
async def test_deepseek_json_fence_is_tolerated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = DeepSeekClient(settings)

    async def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"errors\": []}\n```"}}]})

    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.json_completion("json", "test") == {"errors": []}
    finally:
        await client.close()


def test_telegram_chunks_preserve_content() -> None:
    text = "a" * 4100
    parts = list(chunks(text, limit=3900))
    assert len(parts) == 2
    assert "".join(parts) == text
