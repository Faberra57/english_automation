from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping

from .clients import AnkiConnectClient, DeepSeekClient, ElevenLabsTranscriptionClient, XAITranscriptionClient
from .config import Settings
from .database import Database
from .utils import iso_now


LOG = logging.getLogger("english_teacher.service")


class EnglishTeacherService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database
        self.deepseek = DeepSeekClient(settings)
        self.transcription = XAITranscriptionClient(settings)
        self.elevenlabs_transcription = ElevenLabsTranscriptionClient(settings)
        self.anki = AnkiConnectClient(settings)

    async def close(self) -> None:
        await asyncio.gather(
            self.deepseek.close(),
            self.transcription.close(),
            self.elevenlabs_transcription.close(),
            self.anki.close(),
        )

    async def compare_transcriptions(self, path: Path, mime_type: str | None) -> dict[str, dict[str, Any]]:
        providers = {"xai": self.transcription}
        if self.settings.stt_comparison_enabled:
            providers["elevenlabs"] = self.elevenlabs_transcription

        async def run(provider: str) -> tuple[str, dict[str, Any]]:
            started = time.perf_counter()
            try:
                response = await providers[provider].transcribe(path, mime_type)
                return provider, {
                    "status": "succeeded",
                    "transcript": str(response["text"]).strip(),
                    "response": response,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
            except Exception as exc:
                LOG.warning("Transcription %s échouée: %s", provider, exc)
                return provider, {
                    "status": "failed",
                    "transcript": None,
                    "response": None,
                    "error": str(exc)[:1000],
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }

        results = await asyncio.gather(*(run(provider) for provider in providers))
        return dict(results)

    async def correct(self, submission_id: int, production: str, kind: str) -> dict[str, Any]:
        rag = await self.db.rag_context(production)
        system = f"""You are an English teacher for {self.settings.learner_name}, level {self.settings.learner_level}.
Your style is {self.settings.correction_style}. Write all learner-facing content exclusively in English. Analyze only the submitted production. RAG memories are historical context: never count them as errors made today.
For audio, you only have a transcript: never invent feedback about pronunciation or intonation.
Return only one valid JSON object with exactly this structure:
{{
  "feedback": "helpful feedback in English, plain text",
  "corrected_version": "complete natural English version",
  "advanced_rewrite": "a polished C1-C2 reformulation that preserves the learner's meaning and voice",
  "strengths": ["positive point in English"],
  "errors": [
    {{"category":"short stable category in English", "original":"exact segment", "corrected":"idiomatic correction", "explanation_fr":"explanation in English", "practice_tip":"short tip in English", "severity":3, "confidence":0.95}}
  ]
}}
The corrected_version should fix the production naturally without unnecessarily changing its style. The advanced_rewrite should demonstrate sophisticated, idiomatic C1-C2 English while preserving the original facts, intent, and tone; never invent details.
Identify every real error, including grammar, tense, agreement, articles, prepositions, capitalization, punctuation, spelling, collocation, register, and unnatural word choice. Every correction made in corrected_version must have a matching item in errors; reserve optional stylistic enhancement for advanced_rewrite instead of silently rewriting it. Return up to {self.settings.max_errors} errors, ordered by their position in the production. Never invent errors to reach the maximum; return [] when the production is correct. JSON only."""
        user = f"""PRODUCTION TYPE: {kind}
PRODUCTION TO CORRECT:
---
{production}
---
RAG MEMORY OF PREVIOUS WEAKNESSES:
{rag}

Analyze this production and respond in JSON."""
        result = await self.deepseek.json_completion(system, user)
        raw_errors = result.get("errors", [])
        errors = [error for error in raw_errors if isinstance(error, dict)] if isinstance(raw_errors, list) else []
        result["errors"] = errors
        await self.db.replace_errors(submission_id, errors, self.settings.max_errors)
        result["errors"] = errors[: self.settings.max_errors]
        feedback = self.format_correction(result)
        await self.db.update_submission(
            submission_id,
            status="processed",
            correction_json=json.dumps(result, ensure_ascii=False),
            correction_text=feedback,
            failure_reason=None,
            processed_at=iso_now(),
        )
        return result

    def format_correction(self, result: Mapping[str, Any]) -> str:
        lines = ["Correction", "", str(result.get("feedback", "Here is your feedback."))]
        strengths = result.get("strengths") or []
        if strengths:
            lines.extend(["", "Strengths", *[f"• {item}" for item in strengths]])
        corrected = str(result.get("corrected_version", "")).strip()
        if corrected:
            lines.extend(["", "Natural version", corrected])
        advanced = str(result.get("advanced_rewrite", "")).strip()
        if advanced:
            lines.extend(["", "C1–C2 reformulation", advanced])
        errors = result.get("errors") or []
        if errors:
            lines.extend(["", f"Priority errors ({len(errors)})"])
            for index, error in enumerate(errors, 1):
                lines.extend(
                    [
                        f"{index}. {error.get('category', 'Error')}",
                        f"   {error.get('original', '')} → {error.get('corrected', '')}",
                        f"   {error.get('explanation_fr', '')}",
                    ]
                )
        else:
            lines.extend(["", "No significant errors detected. Well done!"])
        return "\n".join(lines)

    async def make_topic(self, activity_mode: str | None = None) -> tuple[dict[str, Any], str, int]:
        if activity_mode not in {None, "writing", "speaking"}:
            raise ValueError(f"Mode d'activité inconnu: {activity_mode}")
        rag = await self.db.rag_context()
        mode_instruction = {
            None: "Choose the most useful mode between writing and speaking.",
            "writing": "Create a writing exercise only.",
            "speaking": "Create a speaking exercise only.",
        }[activity_mode]
        system = f"""You design a daily English exercise for a learner at level {self.settings.learner_level}.
All learner-facing content must be exclusively in English. Return only this JSON:
{{"title":"short title in English", "mode":"writing or speaking", "prompt":"clear and concrete instructions in English", "focus_points":["focus 1","focus 2"], "starter":"optional opening sentence in English"}}
{mode_instruction}
The exercise should take {self.settings.daily_activity_minutes} minutes, feel motivating, use two or three weaknesses from memory, and vary the context. JSON only."""
        user = f"""Learner interests: {self.settings.learner_interests}
Weaknesses retrieved from RAG memory:
{rag}
Generate today's exercise as JSON."""
        topic = await self.deepseek.json_completion(system, user, max_tokens=1000)
        topic_id = await self.db.save_topic(self.settings.local_date(), topic, rag)
        return topic, rag, topic_id

    def format_topic(self, topic: Mapping[str, Any]) -> str:
        focus = topic.get("focus_points") or []
        mode = str(topic.get("mode", "")).strip()
        heading = (
            f"{mode.title()} exercise — {topic.get('title', 'English practice')}"
            if mode
            else f"Today's exercise — {topic.get('title', 'English practice')}"
        )
        lines = [heading, "", str(topic.get("prompt", ""))]
        if focus:
            lines.extend(["", "Focus points", *[f"• {item}" for item in focus]])
        starter = str(topic.get("starter", "")).strip()
        if starter:
            lines.extend(["", f"Starter: {starter}"])
        topic_mode = str(topic.get("mode", "")).strip().lower()
        instruction = (
            "Reply with a voice note or audio file."
            if topic_mode == "speaking"
            else "Reply with a written message."
            if topic_mode == "writing"
            else "Reply with a written message or a voice note."
        )
        lines.extend(["", instruction])
        return "\n".join(lines)

    async def generate_cards(self, local_date: str, *, source_submission_id: int | None = None) -> int:
        if await self.db.proposal_count(local_date):
            return 0
        context = await self.db.card_generation_context(local_date)
        if not context["submissions"]:
            return 0
        compact_submissions: list[dict[str, Any]] = []
        for submission in context["submissions"]:
            correction = {}
            try:
                correction = json.loads(submission.get("correction_json") or "{}")
            except json.JSONDecodeError:
                pass
            compact_submissions.append(
                {
                    "activity_type": submission.get("activity_type") or submission.get("kind"),
                    "production": submission.get("transcript") or submission.get("raw_text") or "",
                    "corrected_version": correction.get("corrected_version", ""),
                    "advanced_rewrite": correction.get("advanced_rewrite", ""),
                }
            )
        learning_context = {
            "topics": context["topics"],
            "productions": compact_submissions,
            "observed_errors": context["errors"],
        }
        system = """Create exactly 20 high-quality Anki card proposals for an English learner. All content must be in English.
Return only one JSON object with exactly these four arrays and exactly five cards per array:
{
  "theme_vocabulary": [{"front":"recall prompt or contextual gap","back":"advanced word or phrase, definition, and natural example","rationale":"why it fits today's topic","tags":["vocabulary"]}],
  "useful_structure": [{"front":"short completion or transformation prompt","back":"useful formulation or sentence structure with meaning and example","rationale":"when to use it","tags":["structure"]}],
  "grammar_error": [{"front":"prompt based on an observed or closely related grammar weakness","back":"correct form, rule, and example","rationale":"error or weakness addressed","tags":["grammar"]}],
  "vocabulary_error": [{"front":"prompt based on an observed vocabulary, collocation, register, or word-choice weakness","back":"idiomatic correction and example","rationale":"error or weakness addressed","tags":["vocabulary-error"]}]
}
The theme vocabulary should include sophisticated words the learner could naturally have used for today's topic. Useful structures should include reusable formulations such as “as far as … is concerned,” only when contextually appropriate. Grammar and vocabulary-error cards should prioritize actual errors; if fewer than five exist, create adjacent practice cards that target the same demonstrated weaknesses. Cards must be atomic, non-duplicative, practical, and suitable for active recall. The front must not reveal the answer. JSON only."""
        result = await self.deepseek.json_completion(
            system,
            "Create today's 20 proposals from this learning context:\n"
            + json.dumps(learning_context, ensure_ascii=False)[:50000],
            max_tokens=6000,
        )
        categories = ("theme_vocabulary", "useful_structure", "grammar_error", "vocabulary_error")
        groups: dict[str, list[dict[str, Any]]] = {}
        for category in categories:
            raw_cards = result.get(category)
            if not isinstance(raw_cards, list) or len(raw_cards) != 5:
                raise ValueError(f"DeepSeek must return exactly 5 cards for {category}")
            cards: list[dict[str, Any]] = []
            for raw_card in raw_cards:
                if not isinstance(raw_card, dict):
                    raise ValueError(f"Invalid card in {category}")
                front = str(raw_card.get("front") or "").strip()
                back = str(raw_card.get("back") or "").strip()
                if not front or not back:
                    raise ValueError(f"Empty card in {category}")
                tags = raw_card.get("tags") if isinstance(raw_card.get("tags"), list) else []
                cards.append({**raw_card, "front": front, "back": back, "tags": [category, *tags]})
            groups[category] = cards
        return await self.db.create_card_proposals(
            local_date,
            groups,
            source_submission_id=source_submission_id,
        )

    async def push_pending_cards(self, *, local_date: str | None = None) -> tuple[int, int]:
        result = await self.push_pending_cards_detailed(local_date=local_date)
        return int(result["pushed"]), int(result["remaining"])

    async def push_pending_cards_detailed(self, *, local_date: str | None = None) -> dict[str, Any]:
        legacy = (
            [{**card, "_source_table": "cards"} for card in await self.db.pending_cards()]
            if local_date is None
            else []
        )
        proposals = [
            {**card, "_source_table": "card_proposals"}
            for card in await self.db.pending_card_proposals(local_date=local_date)
        ]
        pending = [*legacy, *proposals]
        if not pending or not self.settings.anki_enabled:
            return {
                "pushed": 0,
                "remaining": len(pending),
                "sync_attempted": False,
                "sync_succeeded": False,
                "sync_error": None,
                "anki_enabled": self.settings.anki_enabled,
            }
        pushed = 0
        try:
            await self.anki.ensure_deck()
        except Exception as exc:
            LOG.warning("AnkiConnect indisponible avant envoi: %s", exc)
            return {
                "pushed": 0,
                "remaining": len(pending),
                "sync_attempted": False,
                "sync_succeeded": False,
                "sync_error": f"AnkiConnect unavailable: {str(exc)[:500]}",
                "anki_enabled": True,
            }
        for card in pending:
            try:
                note_id = await self.anki.push_card(card)
                if card["_source_table"] == "card_proposals":
                    await self.db.update_card_proposal(card["id"], status="pushed", note_id=note_id)
                else:
                    await self.db.update_card(card["id"], status="pushed", note_id=note_id)
                pushed += 1
            except Exception as exc:
                LOG.exception("Échec carte Anki %s", card["id"])
                if card["_source_table"] == "card_proposals":
                    await self.db.update_card_proposal(card["id"], status="failed", error=str(exc)[:1000])
                else:
                    await self.db.update_card(card["id"], status="failed", error=str(exc)[:1000])
        sync_attempted = bool(pushed and self.settings.anki_sync_after_push)
        sync_succeeded = False
        sync_error: str | None = None
        if sync_attempted:
            try:
                await self.anki.invoke("sync")
                sync_succeeded = True
            except Exception as exc:
                LOG.warning("Cartes créées mais synchronisation Anki échouée: %s", exc)
                sync_error = str(exc)[:500]
        return {
            "pushed": pushed,
            "remaining": len(pending) - pushed,
            "sync_attempted": sync_attempted,
            "sync_succeeded": sync_succeeded,
            "sync_error": sync_error,
            "anki_enabled": True,
        }
