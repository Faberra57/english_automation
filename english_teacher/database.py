from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

import aiosqlite

from .config import Settings, csv_values
from .utils import bounded_number, iso_now, tokenize, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS activity_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_date TEXT NOT NULL,
    topic_json TEXT NOT NULL,
    rag_context TEXT,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_topics_date ON activity_topics(local_date, sent_at);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text','voice','audio','audio_document')),
    raw_text TEXT,
    transcript TEXT,
    transcription_json TEXT,
    audio_path TEXT,
    audio_original_name TEXT,
    audio_mime_type TEXT,
    audio_sha256 TEXT,
    telegram_file_id TEXT,
    telegram_file_unique_id TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    correction_json TEXT,
    correction_text TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    processed_at TEXT,
    topic_id INTEGER REFERENCES activity_topics(id),
    activity_type TEXT CHECK(activity_type IN ('writing','speaking','journaling')),
    UNIQUE(chat_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_submissions_date ON submissions(local_date, created_at);

CREATE TABLE IF NOT EXISTS transcription_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK(provider IN ('xai','elevenlabs')),
    transcript TEXT,
    response_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('succeeded','failed')),
    error TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(submission_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_transcription_candidates_submission
    ON transcription_candidates(submission_id, provider);

CREATE TABLE IF NOT EXISTS transcription_choices (
    submission_id INTEGER PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES transcription_candidates(id),
    provider TEXT NOT NULL CHECK(provider IN ('xai','elevenlabs')),
    chosen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcription_choices_date ON transcription_choices(chosen_at, provider);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    category TEXT NOT NULL,
    original_text TEXT NOT NULL,
    corrected_text TEXT NOT NULL,
    explanation_fr TEXT NOT NULL,
    practice_tip TEXT,
    severity INTEGER NOT NULL DEFAULT 3,
    confidence REAL NOT NULL DEFAULT 1,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_fingerprint ON errors(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_errors_submission ON errors(submission_id);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_date TEXT NOT NULL,
    topic_json TEXT NOT NULL,
    rag_context TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE(local_date)
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id INTEGER NOT NULL UNIQUE REFERENCES errors(id) ON DELETE CASCADE,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','pushed','failed')),
    anki_note_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    pushed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status, created_at);

CREATE TABLE IF NOT EXISTS card_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_submission_id INTEGER REFERENCES submissions(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN (
        'theme_vocabulary','useful_structure','grammar_error','vocabulary_error'
    )),
    position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 5),
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    rationale TEXT,
    tags_json TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','pending','pushed','failed')),
    anki_note_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    selected_at TEXT,
    pushed_at TEXT,
    UNIQUE(local_date, category, position)
);
CREATE INDEX IF NOT EXISTS idx_card_proposals_date ON card_proposals(local_date, category, position);
CREATE INDEX IF NOT EXISTS idx_card_proposals_queue ON card_proposals(selected, status, created_at);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
"""


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.settings.database_path, timeout=30)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        return db

    async def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.audio_dir.mkdir(parents=True, exist_ok=True)
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = await self.connect()
        try:
            await db.executescript(SCHEMA)
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(submissions)")).fetchall()}
            if "topic_id" not in columns:
                await db.execute("ALTER TABLE submissions ADD COLUMN topic_id INTEGER REFERENCES activity_topics(id)")
            if "activity_type" not in columns:
                await db.execute("ALTER TABLE submissions ADD COLUMN activity_type TEXT")
            proposal_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(card_proposals)")).fetchall()
            }
            if "source_submission_id" not in proposal_columns:
                await db.execute(
                    "ALTER TABLE card_proposals ADD COLUMN source_submission_id INTEGER "
                    "REFERENCES submissions(id) ON DELETE CASCADE"
                )
            await db.commit()
        finally:
            await db.close()

    async def add_submission(self, **values: Any) -> int | None:
        columns = [
            "chat_id", "telegram_user_id", "telegram_message_id", "kind", "raw_text",
            "audio_path", "audio_original_name", "audio_mime_type", "audio_sha256",
            "telegram_file_id", "telegram_file_unique_id", "created_at", "local_date",
            "topic_id", "activity_type",
        ]
        data = [values.get(column) for column in columns]
        db = await self.connect()
        try:
            cursor = await db.execute(
                f"INSERT OR IGNORE INTO submissions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                data,
            )
            await db.commit()
            return int(cursor.lastrowid) if cursor.rowcount else None
        finally:
            await db.close()

    async def update_submission(self, submission_id: int, **values: Any) -> None:
        allowed = {"transcript", "transcription_json", "status", "correction_json", "correction_text", "failure_reason", "processed_at"}
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"Colonnes interdites: {invalid}")
        assignments = ",".join(f"{key}=?" for key in values)
        db = await self.connect()
        try:
            await db.execute(f"UPDATE submissions SET {assignments} WHERE id=?", [*values.values(), submission_id])
            await db.commit()
        finally:
            await db.close()

    async def get_submission(self, submission_id: int) -> dict[str, Any] | None:
        db = await self.connect()
        try:
            row = await (await db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))).fetchone()
            return dict(row) if row else None
        finally:
            await db.close()

    async def replace_transcription_candidates(
        self, submission_id: int, candidates: Mapping[str, Mapping[str, Any]]
    ) -> None:
        db = await self.connect()
        try:
            existing_choice = await (
                await db.execute("SELECT 1 FROM transcription_choices WHERE submission_id=?", (submission_id,))
            ).fetchone()
            if existing_choice:
                raise ValueError("Une transcription a déjà été choisie pour cette production")
            await db.execute("DELETE FROM transcription_candidates WHERE submission_id=?", (submission_id,))
            for provider, candidate in candidates.items():
                if provider not in {"xai", "elevenlabs"}:
                    raise ValueError(f"Fournisseur STT inconnu: {provider}")
                response = candidate.get("response")
                await db.execute(
                    """INSERT INTO transcription_candidates
                       (submission_id,provider,transcript,response_json,status,error,latency_ms,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        submission_id,
                        provider,
                        candidate.get("transcript"),
                        json.dumps(response, ensure_ascii=False) if response is not None else None,
                        candidate.get("status"),
                        candidate.get("error"),
                        candidate.get("latency_ms"),
                        iso_now(),
                    ),
                )
            await db.commit()
        finally:
            await db.close()

    async def submission_exists(self, chat_id: int, message_id: int) -> bool:
        db = await self.connect()
        try:
            row = await (await db.execute(
                "SELECT 1 FROM submissions WHERE chat_id=? AND telegram_message_id=?", (chat_id, message_id)
            )).fetchone()
            return row is not None
        finally:
            await db.close()

    async def replace_errors(self, submission_id: int, errors: Sequence[Mapping[str, Any]], limit: int) -> None:
        now = iso_now()
        db = await self.connect()
        try:
            await db.execute("DELETE FROM errors WHERE submission_id=?", (submission_id,))
            for rank, error in enumerate(errors[:limit], 1):
                original = str(error.get("original", "")).strip()
                corrected = str(error.get("corrected", "")).strip()
                category = str(error.get("category", "autre")).strip().lower()[:100]
                if not original or not corrected or original == corrected:
                    continue
                fingerprint = hashlib.sha256(f"{category}|{original.lower()}|{corrected.lower()}".encode()).hexdigest()[:24]
                await db.execute(
                    """INSERT INTO errors
                    (submission_id,rank,category,original_text,corrected_text,explanation_fr,practice_tip,severity,confidence,fingerprint,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        submission_id, rank, category, original, corrected,
                        str(error.get("explanation_fr", "")).strip(),
                        str(error.get("practice_tip", "")).strip(),
                        int(bounded_number(error.get("severity"), 3, 1, 5)),
                        bounded_number(error.get("confidence"), 1, 0, 1),
                        fingerprint, now,
                    ),
                )
            await db.commit()
        finally:
            await db.close()

    async def rag_context(self, query: str = "") -> str:
        cutoff = (utc_now() - timedelta(days=self.settings.rag_history_days)).isoformat(timespec="seconds")
        db = await self.connect()
        try:
            rows = await (await db.execute(
                """SELECT e.*, s.local_date,
                          COUNT(*) OVER (PARTITION BY e.fingerprint) AS repeat_count,
                          COUNT(*) OVER (PARTITION BY e.category) AS category_count
                   FROM errors e JOIN submissions s ON s.id=e.submission_id
                   WHERE e.created_at>=?
                   ORDER BY e.created_at DESC LIMIT ?""",
                (cutoff, self.settings.rag_candidate_limit),
            )).fetchall()
        finally:
            await db.close()
        query_tokens = tokenize(query)
        now = utc_now()
        ranked: list[tuple[float, aiosqlite.Row]] = []
        for row in rows:
            haystack = " ".join((row["category"], row["original_text"], row["corrected_text"], row["explanation_fr"]))
            overlap = len(query_tokens & tokenize(haystack)) / max(1, len(query_tokens)) if query_tokens else 0
            created = datetime.fromisoformat(row["created_at"])
            age_days = max(0.0, (now - created).total_seconds() / 86400)
            recency = math.exp(-math.log(2) * age_days / self.settings.rag_half_life_days)
            frequency = math.log1p(int(row["repeat_count"])) + (0.5 * math.log1p(int(row["category_count"])))
            score = (4.0 * overlap) + (1.2 * frequency) + recency + (int(row["severity"]) / 5)
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        lines: list[str] = []
        seen: set[str] = set()
        for _, row in ranked:
            if row["fingerprint"] in seen:
                continue
            seen.add(row["fingerprint"])
            lines.append(
                f"- [{row['category']}; motif {row['repeat_count']}x, catégorie {row['category_count']}x; "
                f"sévérité {row['severity']}/5; {row['local_date']}] "
                f"{row['original_text']} → {row['corrected_text']}. {row['explanation_fr']}"
            )
            if len(lines) >= self.settings.rag_top_k:
                break
        result = "\n".join(lines) or "Aucune erreur antérieure: commencer par un diagnostic général."
        return result[: self.settings.rag_max_context_chars]

    async def save_topic(self, local_date: str, topic: Mapping[str, Any], rag_context: str) -> int:
        db = await self.connect()
        try:
            topic_json = json.dumps(topic, ensure_ascii=False)
            sent_at = iso_now()
            cursor = await db.execute(
                "INSERT INTO activity_topics(local_date,topic_json,rag_context,sent_at) VALUES(?,?,?,?)",
                (local_date, topic_json, rag_context, sent_at),
            )
            await db.execute(
                "INSERT INTO topics(local_date,topic_json,rag_context,sent_at) VALUES(?,?,?,?) "
                "ON CONFLICT(local_date) DO UPDATE SET topic_json=excluded.topic_json,rag_context=excluded.rag_context,sent_at=excluded.sent_at",
                (local_date, topic_json, rag_context, sent_at),
            )
            await db.commit()
            return int(cursor.lastrowid)
        finally:
            await db.close()

    async def uncarded_errors(self, local_date: str, limit: int) -> list[dict[str, Any]]:
        db = await self.connect()
        try:
            rows = await (await db.execute(
                """SELECT e.* FROM errors e JOIN submissions s ON s.id=e.submission_id
                   LEFT JOIN cards c ON c.error_id=e.id
                   WHERE s.local_date=? AND c.id IS NULL
                   ORDER BY e.severity DESC,e.rank ASC,e.created_at DESC LIMIT ?""",
                (local_date, limit),
            )).fetchall()
            return [dict(row) for row in rows]
        finally:
            await db.close()

    async def create_cards(self, cards: Sequence[Mapping[str, Any]]) -> int:
        db = await self.connect()
        created = 0
        try:
            for card in cards:
                raw_tags = card.get("tags", [])
                if isinstance(raw_tags, str):
                    raw_tags = csv_values(raw_tags)
                elif not isinstance(raw_tags, (list, tuple)):
                    raw_tags = []
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO cards(error_id,front,back,tags_json,created_at) VALUES(?,?,?,?,?)",
                    (
                        int(card["error_id"]), str(card["front"]).strip(), str(card["back"]).strip(),
                        json.dumps([str(tag) for tag in raw_tags if str(tag).strip()], ensure_ascii=False), iso_now(),
                    ),
                )
                created += max(0, cursor.rowcount)
            await db.commit()
            return created
        finally:
            await db.close()

    async def card_generation_context(self, local_date: str) -> dict[str, Any]:
        db = await self.connect()
        try:
            topics = await (await db.execute(
                "SELECT topic_json FROM activity_topics WHERE local_date=? ORDER BY sent_at",
                (local_date,),
            )).fetchall()
            submissions = await (await db.execute(
                """SELECT id,kind,activity_type,raw_text,transcript,correction_json
                   FROM submissions WHERE local_date=? AND status='processed' ORDER BY created_at""",
                (local_date,),
            )).fetchall()
            errors = await (await db.execute(
                """SELECT e.category,e.original_text,e.corrected_text,e.explanation_fr,e.practice_tip,e.severity
                   FROM errors e JOIN submissions s ON s.id=e.submission_id
                   WHERE s.local_date=? ORDER BY e.severity DESC,e.rank""",
                (local_date,),
            )).fetchall()
            return {
                "topics": [json.loads(row["topic_json"]) for row in topics],
                "submissions": [dict(row) for row in submissions],
                "errors": [dict(row) for row in errors],
            }
        finally:
            await db.close()

    async def proposal_count(self, local_date: str) -> int:
        db = await self.connect()
        try:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM card_proposals WHERE local_date=?", (local_date,)
            )).fetchone()
            return int(row[0])
        finally:
            await db.close()

    async def create_card_proposals(
        self,
        local_date: str,
        groups: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        source_submission_id: int | None = None,
    ) -> int:
        created = 0
        db = await self.connect()
        try:
            for category, cards in groups.items():
                for position, card in enumerate(cards, 1):
                    raw_tags = card.get("tags") or []
                    if not isinstance(raw_tags, (list, tuple)):
                        raw_tags = []
                    cursor = await db.execute(
                        """INSERT OR IGNORE INTO card_proposals
                           (source_submission_id,local_date,category,position,front,back,rationale,tags_json,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            source_submission_id,
                            local_date,
                            category,
                            position,
                            str(card.get("front") or "").strip(),
                            str(card.get("back") or "").strip(),
                            str(card.get("rationale") or "").strip(),
                            json.dumps([str(tag) for tag in raw_tags if str(tag).strip()], ensure_ascii=False),
                            iso_now(),
                        ),
                    )
                    created += max(0, cursor.rowcount)
            await db.commit()
            return created
        finally:
            await db.close()

    async def pending_card_proposals(
        self, limit: int = 100, *, local_date: str | None = None
    ) -> list[dict[str, Any]]:
        db = await self.connect()
        try:
            date_clause = " AND local_date=?" if local_date else ""
            params: tuple[Any, ...] = (local_date, limit) if local_date else (limit,)
            rows = await (
                await db.execute(
                    f"""SELECT * FROM card_proposals
                       WHERE selected=1 AND status IN ('pending','failed'){date_clause}
                       ORDER BY selected_at,created_at,id LIMIT ?""",
                    params,
                )
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            await db.close()

    async def update_card_proposal(
        self, proposal_id: int, *, status: str, note_id: int | None = None, error: str | None = None
    ) -> None:
        db = await self.connect()
        try:
            await db.execute(
                """UPDATE card_proposals SET status=?,anki_note_id=COALESCE(?,anki_note_id),
                   attempts=attempts+1,last_error=?,
                   pushed_at=CASE WHEN ?='pushed' THEN ? ELSE pushed_at END WHERE id=?""",
                (status, note_id, error, status, iso_now(), proposal_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def pending_cards(self, limit: int = 100) -> list[dict[str, Any]]:
        db = await self.connect()
        try:
            rows = await (await db.execute(
                "SELECT * FROM cards WHERE status IN ('pending','failed') ORDER BY created_at,id LIMIT ?", (limit,)
            )).fetchall()
            return [dict(row) for row in rows]
        finally:
            await db.close()

    async def update_card(self, card_id: int, *, status: str, note_id: int | None = None, error: str | None = None) -> None:
        db = await self.connect()
        try:
            await db.execute(
                """UPDATE cards SET status=?,anki_note_id=COALESCE(?,anki_note_id),attempts=attempts+1,
                   last_error=?,pushed_at=CASE WHEN ?='pushed' THEN ? ELSE pushed_at END WHERE id=?""",
                (status, note_id, error, status, iso_now(), card_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def stats(self) -> dict[str, int]:
        db = await self.connect()
        try:
            result: dict[str, int] = {}
            for key, query in {
                "submissions": "SELECT COUNT(*) FROM submissions",
                "audio": "SELECT COUNT(*) FROM submissions WHERE audio_path IS NOT NULL",
                "errors": "SELECT COUNT(*) FROM errors",
                "cards_pending": "SELECT (SELECT COUNT(*) FROM cards WHERE status!='pushed') + "
                                 "(SELECT COUNT(*) FROM card_proposals WHERE selected=1 AND status!='pushed')",
                "cards_pushed": "SELECT (SELECT COUNT(*) FROM cards WHERE status='pushed') + "
                                "(SELECT COUNT(*) FROM card_proposals WHERE status='pushed')",
            }.items():
                result[key] = int((await (await db.execute(query)).fetchone())[0])
            return result
        finally:
            await db.close()
