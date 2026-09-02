from __future__ import annotations

import asyncio
import html
import json
from pathlib import Path
from typing import Any, Mapping

import httpx

from .config import Settings
from .utils import clean_json_text


class ApiError(RuntimeError):
    pass


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int,
    **kwargs: Any,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                raise ApiError(f"HTTP {response.status_code}: {response.text[:500]}")
            response.raise_for_status()
            return response
        except (httpx.HTTPError, ApiError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            await asyncio.sleep(min(8, 2 ** (attempt - 1)))
    raise ApiError(str(last_error) if last_error else "Échec API inconnu")


class DeepSeekClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.deepseek_timeout),
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def json_completion(self, system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.settings.deepseek_temperature,
            "max_tokens": max_tokens or self.settings.deepseek_max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if self.settings.deepseek_reasoning_effort:
            body["reasoning_effort"] = self.settings.deepseek_reasoning_effort
        body["thinking"] = {"type": "enabled" if self.settings.deepseek_thinking_enabled else "disabled"}
        last_error: Exception | None = None
        last_finish_reason = "unknown"
        last_content_length = 0
        for attempt in range(2):
            response = await request_with_retries(
                self.client,
                "POST",
                f"{self.settings.deepseek_base_url}/chat/completions",
                retries=self.settings.deepseek_max_retries,
                json=body,
            )
            try:
                payload = response.json()
                choice = payload["choices"][0]
                last_finish_reason = str(choice.get("finish_reason") or "unknown")
                content = choice["message"]["content"]
                last_content_length = len(content or "")
                if not content:
                    raise ValueError("réponse vide")
                parsed = json.loads(clean_json_text(content))
                if not isinstance(parsed, dict):
                    raise ValueError("la racine JSON n'est pas un objet")
                return parsed
            except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    body["max_tokens"] = int(body["max_tokens"]) * 2
                    body["messages"][1]["content"] += (
                        "\nIMPORTANT: return one complete valid JSON object only, with no Markdown fence."
                    )
        raise ApiError(
            f"Invalid DeepSeek JSON: {last_error}; finish_reason={last_finish_reason}; "
            f"content_length={last_content_length}"
        )


class XAITranscriptionClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.xai_stt_timeout),
            headers={"Authorization": f"Bearer {settings.xai_api_key}"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def transcribe(self, path: Path, mime_type: str | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.settings.xai_stt_max_retries + 1):
            try:
                with path.open("rb") as audio:
                    response = await self.client.post(
                        self.settings.xai_stt_url,
                        data={
                            "format": str(self.settings.xai_stt_format).lower(),
                            "language": self.settings.xai_stt_language,
                        },
                        files={"file": (path.name, audio, mime_type or "application/octet-stream")},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise ApiError(f"HTTP {response.status_code}: {response.text[:500]}")
                response.raise_for_status()
                result = response.json()
                if not str(result.get("text", "")).strip():
                    raise ApiError("Transcription xAI vide")
                return result
            except (httpx.HTTPError, ApiError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.settings.xai_stt_max_retries:
                    await asyncio.sleep(min(8, 2 ** (attempt - 1)))
        raise ApiError(f"Échec transcription xAI: {last_error}")


class ElevenLabsTranscriptionClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.elevenlabs_stt_timeout),
            headers={"xi-api-key": settings.elevenlabs_api_key},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def transcribe(self, path: Path, mime_type: str | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.settings.elevenlabs_stt_max_retries + 1):
            try:
                with path.open("rb") as audio:
                    response = await self.client.post(
                        self.settings.elevenlabs_stt_url,
                        data={
                            "model_id": self.settings.elevenlabs_stt_model,
                            "language_code": self.settings.elevenlabs_stt_language,
                            "tag_audio_events": "false",
                            "diarize": "false",
                        },
                        files={"file": (path.name, audio, mime_type or "application/octet-stream")},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise ApiError(f"HTTP {response.status_code}: {response.text[:500]}")
                response.raise_for_status()
                result = response.json()
                if not str(result.get("text", "")).strip():
                    raise ApiError("Transcription ElevenLabs vide")
                return result
            except (httpx.HTTPError, ApiError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.settings.elevenlabs_stt_max_retries:
                    await asyncio.sleep(min(8, 2 ** (attempt - 1)))
        raise ApiError(f"Échec transcription ElevenLabs: {last_error}")


class AnkiConnectClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.anki_timeout))

    async def close(self) -> None:
        await self.client.aclose()

    async def invoke(self, action: str, **params: Any) -> Any:
        payload: dict[str, Any] = {"action": action, "version": 6}
        if params:
            payload["params"] = params
        if self.settings.ankiconnect_api_key:
            payload["key"] = self.settings.ankiconnect_api_key
        response = await self.client.post(self.settings.ankiconnect_url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "error" not in data or "result" not in data:
            raise ApiError("Réponse AnkiConnect malformée")
        if data["error"]:
            raise ApiError(f"AnkiConnect {action}: {data['error']}")
        return data["result"]

    async def ensure_deck(self) -> None:
        if self.settings.anki_create_deck:
            await self.invoke("createDeck", deck=self.settings.anki_deck)

    async def push_card(self, card: Mapping[str, Any]) -> int:
        source = str(card.get("_source_table") or "cards")
        unique_tag = f"english_ai_{source}_{card['id']}"
        existing = await self.invoke("findNotes", query=f"tag:{unique_tag}")
        if existing:
            return int(existing[0])
        tags = list(dict.fromkeys([*self.settings.anki_tags, *json.loads(card["tags_json"]), unique_tag]))
        note = {
            "deckName": self.settings.anki_deck,
            "modelName": self.settings.anki_model,
            "fields": {
                self.settings.anki_front_field: html.escape(str(card["front"])).replace("\n", "<br>"),
                self.settings.anki_back_field: html.escape(str(card["back"])).replace("\n", "<br>"),
            },
            "options": {"allowDuplicate": False, "duplicateScope": self.settings.anki_duplicate_scope},
            "tags": tags,
        }
        note_id = await self.invoke("addNote", note=note)
        if note_id is None:
            raise ApiError("AnkiConnect n'a pas créé la note")
        return int(note_id)
