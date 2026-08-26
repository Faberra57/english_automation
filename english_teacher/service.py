from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

from .clients import AnkiConnectClient, DeepSeekClient, GroqClient
from .config import Settings
from .database import Database
from .utils import iso_now


LOG = logging.getLogger("english_teacher.service")


class EnglishTeacherService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database
        self.deepseek = DeepSeekClient(settings)
        self.groq = GroqClient(settings)
        self.anki = AnkiConnectClient(settings)

    async def close(self) -> None:
        await asyncio.gather(self.deepseek.close(), self.groq.close(), self.anki.close())

    async def correct(self, submission_id: int, production: str, kind: str) -> dict[str, Any]:
        rag = await self.db.rag_context(production)
        system = f"""Tu es professeur de {self.settings.target_language} pour {self.settings.learner_name}, niveau {self.settings.learner_level}.
Ton style est {self.settings.correction_style}. Analyse uniquement la production fournie. Les souvenirs RAG sont un historique: ne les compte pas comme des erreurs présentes aujourd'hui.
Pour un audio, tu ne disposes que de la transcription: n'invente aucune évaluation de prononciation ou d'intonation.
Retourne exclusivement un objet JSON valide ayant exactement cette structure:
{{
  "feedback": "bilan utile en français, texte brut",
  "corrected_version": "version anglaise naturelle complète",
  "strengths": ["point positif"],
  "errors": [
    {{"category":"catégorie stable et courte", "original":"segment exact", "corrected":"correction idiomatique", "explanation_fr":"explication en français", "practice_tip":"mini conseil", "severity":3, "confidence":0.95}}
  ]
}}
Maximum {self.settings.max_errors} erreurs, classées par impact pédagogique. N'invente pas d'erreur pour atteindre le maximum; retourne [] si tout est correct. JSON seulement."""
        user = f"""TYPE DE PRODUCTION: {kind}
PRODUCTION À CORRIGER:
---
{production}
---
MÉMOIRE RAG DES FAIBLESSES PASSÉES:
{rag}

Analyse cette production et réponds en JSON."""
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
        lines = ["Correction", "", str(result.get("feedback", "Voici ton bilan."))]
        strengths = result.get("strengths") or []
        if strengths:
            lines.extend(["", "Points forts", *[f"• {item}" for item in strengths]])
        corrected = str(result.get("corrected_version", "")).strip()
        if corrected:
            lines.extend(["", "Version naturelle", corrected])
        errors = result.get("errors") or []
        if errors:
            lines.extend(["", f"Erreurs prioritaires ({len(errors)})"])
            for index, error in enumerate(errors, 1):
                lines.extend(
                    [
                        f"{index}. {error.get('category', 'Erreur')}",
                        f"   {error.get('original', '')} → {error.get('corrected', '')}",
                        f"   {error.get('explanation_fr', '')}",
                    ]
                )
        else:
            lines.extend(["", "Aucune erreur notable détectée. Bravo !"])
        return "\n".join(lines)

    async def make_topic(self) -> tuple[dict[str, Any], str]:
        rag = await self.db.rag_context()
        system = f"""Tu conçois un exercice quotidien d'anglais pour un francophone de niveau {self.settings.learner_level}.
Retourne exclusivement ce JSON:
{{"title":"titre court", "mode":"oral ou écrit", "prompt":"consigne concrète en français avec la situation à produire en anglais", "focus_points":["cible 1","cible 2"], "starter":"première phrase anglaise facultative"}}
L'exercice doit durer {self.settings.daily_activity_minutes} minutes, être motivant, exploiter 2 ou 3 faiblesses de la mémoire et varier le contexte. JSON seulement."""
        user = f"""Centres d'intérêt: {self.settings.learner_interests}
Faiblesses récupérées depuis la mémoire RAG:
{rag}
Génère le sujet du jour en JSON."""
        topic = await self.deepseek.json_completion(system, user, max_tokens=1000)
        await self.db.save_topic(self.settings.local_date(), topic, rag)
        return topic, rag

    def format_topic(self, topic: Mapping[str, Any]) -> str:
        focus = topic.get("focus_points") or []
        lines = [f"Sujet du jour — {topic.get('title', 'English practice')}", "", str(topic.get("prompt", ""))]
        if focus:
            lines.extend(["", "À travailler", *[f"• {item}" for item in focus]])
        starter = str(topic.get("starter", "")).strip()
        if starter:
            lines.extend(["", f"Pour démarrer : {starter}"])
        lines.extend(["", "Réponds ici par texte ou note vocale."])
        return "\n".join(lines)

    async def generate_cards(self, local_date: str) -> int:
        errors = await self.db.uncarded_errors(local_date, self.settings.daily_card_limit)
        if not errors:
            return 0
        compact_errors = [
            {
                "error_id": error["id"],
                "category": error["category"],
                "original": error["original_text"],
                "corrected": error["corrected_text"],
                "explanation_fr": error["explanation_fr"],
            }
            for error in errors
        ]
        system = """Tu crées des fiches Anki atomiques pour un francophone apprenant l'anglais.
Retourne exclusivement un objet JSON de forme:
{"cards":[{"error_id":123,"front":"concept ou consigne très courte en français","back":"phrase anglaise idiomatique complète suivie d'une brève explication française","tags":["grammar"]}]}
Conserve chaque error_id fourni exactement une fois. La face avant ne doit pas révéler la réponse anglaise. La face arrière doit réemployer la correction dans une phrase mémorisable. JSON seulement."""
        result = await self.deepseek.json_completion(
            system,
            "Transforme ces erreurs en fiches:\n" + json.dumps(compact_errors, ensure_ascii=False),
            max_tokens=1800,
        )
        valid_ids = {int(error["id"]) for error in errors}
        cards: list[dict[str, Any]] = []
        for raw_card in result.get("cards") or []:
            if not isinstance(raw_card, dict):
                continue
            try:
                error_id = int(raw_card.get("error_id"))
            except (TypeError, ValueError):
                continue
            if error_id not in valid_ids or not str(raw_card.get("front", "")).strip() or not str(raw_card.get("back", "")).strip():
                continue
            cards.append({**raw_card, "error_id": error_id})
        return await self.db.create_cards(cards)

    async def push_pending_cards(self) -> tuple[int, int]:
        pending = await self.db.pending_cards()
        if not pending or not self.settings.anki_enabled:
            return 0, len(pending)
        pushed = 0
        try:
            await self.anki.ensure_deck()
        except Exception as exc:
            LOG.warning("AnkiConnect indisponible avant envoi: %s", exc)
            return 0, len(pending)
        for card in pending:
            try:
                note_id = await self.anki.push_card(card)
                await self.db.update_card(card["id"], status="pushed", note_id=note_id)
                pushed += 1
            except Exception as exc:
                LOG.exception("Échec carte Anki %s", card["id"])
                await self.db.update_card(card["id"], status="failed", error=str(exc)[:1000])
        if pushed and self.settings.anki_sync_after_push:
            try:
                await self.anki.invoke("sync")
            except Exception as exc:
                LOG.warning("Cartes créées mais synchronisation Anki échouée: %s", exc)
        return pushed, len(pending) - pushed

