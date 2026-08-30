from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from english_teacher.clients import DeepSeekClient
from english_teacher.config import Settings, parse_clock, parse_days
from english_teacher.database import Database
from english_teacher.dashboard import annotate_text, annotate_text_diff
from english_teacher.dashboard_data import DashboardRepository
from english_teacher.service import EnglishTeacherService
from english_teacher.main import RedactSecretsFilter
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


def test_input_mode_defaults_to_both_and_validates(tmp_path: Path) -> None:
    assert make_settings(tmp_path).input_mode == "both"
    assert make_settings(tmp_path, INPUT_MODE="write_only").input_mode == "write_only"
    assert make_settings(tmp_path, INPUT_MODE=" AUDIO_ONLY ").input_mode == "audio_only"
    with pytest.raises(ValueError, match="INPUT_MODE invalide"):
        make_settings(tmp_path, INPUT_MODE="text")


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
        assert sent["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"errors\": []}\n```"}}]})

    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.json_completion("json", "test") == {"errors": []}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deepseek_retries_empty_content_with_larger_budget(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DEEPSEEK_MAX_TOKENS="500")
    client = DeepSeekClient(settings)
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        requests.append(sent)
        content = "" if len(requests) == 1 else '{"errors": []}'
        finish_reason = "length" if len(requests) == 1 else "stop"
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]},
        )

    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.json_completion("json", "test") == {"errors": []}
        assert requests[0]["max_tokens"] == 500
        assert requests[1]["max_tokens"] == 1000
    finally:
        await client.close()


def test_telegram_chunks_preserve_content() -> None:
    text = "a" * 4100
    parts = list(chunks(text, limit=3900))
    assert len(parts) == 2
    assert "".join(parts) == text


@pytest.mark.asyncio
async def test_manual_topic_mode_is_forced(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    service = EnglishTeacherService(settings, db)
    prompts: list[str] = []

    async def fake_completion(system: str, user: str, **kwargs: object) -> dict[str, object]:
        prompts.append(system)
        mode = "writing" if "writing exercise only" in system else "speaking"
        return {"title": "Test", "mode": mode, "prompt": "Practice", "focus_points": []}

    service.deepseek.json_completion = fake_completion  # type: ignore[method-assign]
    try:
        writing, _, writing_topic_id = await service.make_topic("writing")
        speaking, _, speaking_topic_id = await service.make_topic("speaking")
        assert writing["mode"] == "writing"
        assert speaking["mode"] == "speaking"
        assert writing_topic_id != speaking_topic_id
        assert "Create a writing exercise only" in prompts[0]
        assert "Create a speaking exercise only" in prompts[1]
        assert "exclusively in English" in prompts[0]
        assert "Writing exercise" in service.format_topic(writing)
        assert "Focus points" not in service.format_topic(writing)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_dashboard_reads_journal_and_statistics(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    topic_id = await db.save_topic(
        "2026-08-28",
        {"title": "Yesterday", "mode": "writing", "prompt": "Raconte ta journée."},
        "test",
    )
    submission_id = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=99,
        kind="text",
        raw_text="I go yesterday.",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-28T08:00:00+00:00",
        local_date="2026-08-28",
        topic_id=topic_id,
    )
    assert submission_id is not None
    await db.update_submission(
        submission_id,
        status="processed",
        correction_json=json.dumps(
            {"feedback": "Bien essayé", "corrected_version": "I went yesterday.", "strengths": []}
        ),
    )
    await db.replace_errors(
        submission_id,
        [
            {
                "category": "past tense",
                "original": "go",
                "corrected": "went",
                "explanation_fr": "Utilise le prétérit.",
                "severity": 4,
            }
        ],
        5,
    )
    journal_id = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=100,
        kind="text",
        raw_text="Today was calm and productive.",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-28T20:00:00+00:00",
        local_date="2026-08-28",
        activity_type="journaling",
    )
    assert journal_id is not None
    await db.update_submission(journal_id, status="processed", correction_json='{"errors": []}')
    repository = DashboardRepository(settings.database_path)
    journal = repository.journal(
        start_date=date.fromisoformat("2026-08-28"),
        end_date=date.fromisoformat("2026-08-28"),
        search="Yesterday",
    )
    assert len(journal["submissions"]) == 1
    assert journal["submissions"][0]["topic"]["mode"] == "writing"
    assert journal["submissions"][0]["errors"][0]["corrected_text"] == "went"
    statistics = repository.statistics()
    assert statistics["summary"]["submissions"] == 2
    assert statistics["summary"]["journal_entries"] == 1
    assert statistics["summary"]["journaling_words"] == 5
    assert statistics["summary"]["errors"] == 1
    assert statistics["categories"][0]["category"] == "past tense"


def test_correction_output_labels_are_english(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    service = EnglishTeacherService(settings, Database(settings))
    try:
        rendered = service.format_correction(
            {
                "feedback": "Good effort.",
                "strengths": ["Clear idea"],
                "corrected_version": "I went home.",
                "advanced_rewrite": "I made my way home.",
                "errors": [],
            }
        )
        assert "Strengths" in rendered
        assert "Natural version" in rendered
        assert "C1–C2 reformulation" in rendered
        assert "No significant errors detected" in rendered
        assert "Points forts" not in rendered
    finally:
        import asyncio

        asyncio.run(service.close())


def test_dashboard_annotates_errors_and_escapes_html() -> None:
    rendered = annotate_text(
        "I <go> home.\nThen I sleep.",
        [
            {"original_text": "<go>", "corrected_text": "went"},
            {"original_text": "sleep", "corrected_text": "slept"},
        ],
    )
    assert '<span class="inline-error">&lt;go&gt;</span>' in rendered
    assert '<ins class="inline-fix">went</ins>' in rendered
    assert '<span class="inline-error">sleep</span>' in rendered
    assert '<ins class="inline-fix">slept</ins>' in rendered
    assert "<br>" in rendered
    assert "I <go>" not in rendered

    diff_rendered = annotate_text_diff("i go home", "I went home")
    assert '<span class="inline-error">i</span>' in diff_rendered
    assert '<ins class="inline-fix">I</ins>' in diff_rendered
    assert '<span class="inline-error">go</span>' in diff_rendered
    assert '<ins class="inline-fix">went</ins>' in diff_rendered


def test_logs_redact_telegram_bot_tokens() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "POST https://api.telegram.org/bot123456:secret_TOKEN/getMe",
        (),
        None,
    )
    assert RedactSecretsFilter().filter(record)
    assert record.getMessage() == "POST https://api.telegram.org/bot<redacted>/getMe"
    assert "secret_TOKEN" not in record.getMessage()


@pytest.mark.asyncio
async def test_card_proposals_are_grouped_selected_and_retained(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings)
    await db.initialize()
    submission_id = await db.add_submission(
        chat_id=42,
        telegram_user_id=42,
        telegram_message_id=120,
        kind="text",
        raw_text="I discussed my work today.",
        audio_path=None,
        audio_original_name=None,
        audio_mime_type=None,
        audio_sha256=None,
        telegram_file_id=None,
        telegram_file_unique_id=None,
        created_at="2026-08-28T20:00:00+00:00",
        local_date="2026-08-28",
        activity_type="writing",
    )
    assert submission_id is not None
    await db.update_submission(submission_id, status="processed", correction_json='{"errors": []}')
    service = EnglishTeacherService(settings, db)

    async def fake_completion(system: str, user: str, **kwargs: object) -> dict[str, object]:
        assert "exactly 20" in system
        assert kwargs["max_tokens"] == 6000
        return {
            category: [
                {
                    "front": f"{category} prompt {position}",
                    "back": f"{category} answer {position}",
                    "rationale": "Useful today",
                    "tags": ["test"],
                }
                for position in range(1, 6)
            ]
            for category in (
                "theme_vocabulary",
                "useful_structure",
                "grammar_error",
                "vocabulary_error",
            )
        }

    service.deepseek.json_completion = fake_completion  # type: ignore[method-assign]
    try:
        assert await service.generate_cards("2026-08-28") == 20
        assert await service.generate_cards("2026-08-28") == 0
        repository = DashboardRepository(settings.database_path)
        proposals = repository.card_proposals("2026-08-28")
        assert len(proposals) == 20
        assert {card["category"] for card in proposals} == {
            "theme_vocabulary",
            "useful_structure",
            "grammar_error",
            "vocabulary_error",
        }
        with pytest.raises(ValueError, match="entre 5 et 10"):
            repository.save_card_selection("2026-08-28", {card["id"] for card in proposals[:4]})
        chosen = {card["id"] for card in proposals[:7]}
        assert repository.save_card_selection("2026-08-28", chosen) == 7
        refreshed = repository.card_proposals("2026-08-28")
        assert sum(card["selected"] for card in refreshed) == 7
        assert sum(card["status"] == "proposed" for card in refreshed) == 13
        assert len(await db.pending_card_proposals()) == 7
        assert await service.push_pending_cards() == (0, 7)
    finally:
        await service.close()
