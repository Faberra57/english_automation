from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class DashboardRepository:
    """Read-only queries used by the Streamlit dashboard."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Base SQLite introuvable: {self.database_path}")
        connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _connect_writable(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Base SQLite introuvable: {self.database_path}")
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def date_bounds(self) -> tuple[date, date]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(local_date) AS first_date, MAX(local_date) AS last_date FROM submissions"
            ).fetchone()
        today = date.today()
        if not row or not row["first_date"]:
            return today, today
        return date.fromisoformat(row["first_date"]), date.fromisoformat(row["last_date"])

    def journal(
        self,
        *,
        start_date: date,
        end_date: date,
        kinds: Iterable[str] = (),
        activity_types: Iterable[str] = (),
        statuses: Iterable[str] = (),
        search: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        clauses = ["s.local_date BETWEEN ? AND ?"]
        params: list[Any] = [start_date.isoformat(), end_date.isoformat()]
        selected_kinds = tuple(kinds)
        selected_activities = tuple(activity_types)
        selected_statuses = tuple(statuses)
        if selected_kinds:
            clauses.append(f"s.kind IN ({','.join('?' for _ in selected_kinds)})")
            params.extend(selected_kinds)
        if selected_activities:
            clauses.append(
                f"COALESCE(s.activity_type, CASE WHEN s.kind='text' THEN 'writing' ELSE 'speaking' END) "
                f"IN ({','.join('?' for _ in selected_activities)})"
            )
            params.extend(selected_activities)
        if selected_statuses:
            clauses.append(f"s.status IN ({','.join('?' for _ in selected_statuses)})")
            params.extend(selected_statuses)
        normalized_search = search.strip()
        if normalized_search:
            clauses.append(
                "(COALESCE(s.raw_text,'') LIKE ? OR COALESCE(s.transcript,'') LIKE ? "
                "OR COALESCE(s.correction_text,'') LIKE ? "
                "OR COALESCE(a.topic_json, CASE WHEN s.activity_type IS NULL THEN t.topic_json END, '') LIKE ?)"
            )
            pattern = f"%{normalized_search}%"
            params.extend([pattern] * 4)
        params.append(max(1, min(limit, 1000)))
        query = f"""
            SELECT s.*,
                   COALESCE(a.topic_json, CASE WHEN s.activity_type IS NULL THEN t.topic_json END) AS topic_json,
                   (SELECT COUNT(*) FROM errors e WHERE e.submission_id=s.id) AS error_count,
                   (SELECT COUNT(*) FROM errors e JOIN cards c ON c.error_id=e.id WHERE e.submission_id=s.id) AS card_count
            FROM submissions s
            LEFT JOIN activity_topics a ON a.id=s.topic_id
            LEFT JOIN topics t ON t.local_date=s.local_date
            WHERE {' AND '.join(clauses)}
            ORDER BY s.local_date DESC, s.created_at DESC, s.id DESC
            LIMIT ?
        """
        with self._connect() as connection:
            submissions = [dict(row) for row in connection.execute(query, params).fetchall()]
            submission_ids = [int(item["id"]) for item in submissions]
            errors_by_submission: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in submission_ids}
            if submission_ids:
                placeholders = ",".join("?" for _ in submission_ids)
                detail_rows = connection.execute(
                    f"""
                    SELECT e.*, c.id AS card_id, c.front AS card_front, c.back AS card_back,
                           c.tags_json AS card_tags_json, c.status AS card_status,
                           c.anki_note_id, c.attempts AS card_attempts, c.last_error AS card_last_error
                    FROM errors e
                    LEFT JOIN cards c ON c.error_id=e.id
                    WHERE e.submission_id IN ({placeholders})
                    ORDER BY e.submission_id, e.rank, e.id
                    """,
                    submission_ids,
                ).fetchall()
                for row in detail_rows:
                    detail = dict(row)
                    detail["card_tags"] = parse_json(detail.pop("card_tags_json", None), [])
                    errors_by_submission[int(detail["submission_id"])].append(detail)

        for submission in submissions:
            submission["topic"] = parse_json(submission.pop("topic_json", None), {})
            submission["correction"] = parse_json(submission.get("correction_json"), {})
            submission["transcription"] = parse_json(submission.get("transcription_json"), {})
            submission["errors"] = errors_by_submission[int(submission["id"])]
        return {"submissions": submissions, "truncated": len(submissions) >= params[-1]}

    def statistics(self) -> dict[str, Any]:
        with self._connect() as connection:
            summary = dict(
                connection.execute(
                    """
                    SELECT COUNT(*) AS submissions,
                           COUNT(DISTINCT local_date) AS active_days,
                           SUM(CASE WHEN kind='text' AND COALESCE(activity_type,'writing')='writing' THEN 1 ELSE 0 END) AS writings,
                           SUM(CASE WHEN activity_type='journaling' THEN 1 ELSE 0 END) AS journal_entries,
                           SUM(CASE WHEN kind!='text' THEN 1 ELSE 0 END) AS audios,
                           SUM(CASE WHEN status='processed' THEN 1 ELSE 0 END) AS processed,
                           SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                    FROM submissions
                    """
                ).fetchone()
            )
            totals = dict(
                connection.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM errors) AS errors,
                           ((SELECT COUNT(*) FROM cards) + (SELECT COUNT(*) FROM card_proposals)) AS cards,
                           ((SELECT COUNT(*) FROM cards WHERE status='pushed') +
                            (SELECT COUNT(*) FROM card_proposals WHERE status='pushed')) AS cards_pushed,
                           MAX((SELECT COUNT(*) FROM activity_topics), (SELECT COUNT(*) FROM topics)) AS topics,
                           (SELECT COUNT(*) FROM submissions s
                            WHERE s.status='processed' AND NOT EXISTS
                                  (SELECT 1 FROM errors e WHERE e.submission_id=s.id)) AS flawless
                    """
                ).fetchone()
            )
            daily = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT s.local_date AS day,
                           COUNT(DISTINCT s.id) AS submissions,
                           COUNT(DISTINCT CASE WHEN s.kind='text' AND COALESCE(s.activity_type,'writing')='writing' THEN s.id END) AS writings,
                           COUNT(DISTINCT CASE WHEN s.activity_type='journaling' THEN s.id END) AS journal_entries,
                           COUNT(DISTINCT CASE WHEN s.kind!='text' THEN s.id END) AS audios,
                           COUNT(e.id) AS errors
                    FROM submissions s LEFT JOIN errors e ON e.submission_id=s.id
                    GROUP BY s.local_date ORDER BY s.local_date
                    """
                ).fetchall()
            ]
            categories = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT category, COUNT(*) AS occurrences, ROUND(AVG(severity), 2) AS avg_severity
                    FROM errors GROUP BY category ORDER BY occurrences DESC, avg_severity DESC LIMIT 12
                    """
                ).fetchall()
            ]
            card_statuses = [
                dict(row)
                for row in connection.execute(
                    """SELECT display_status AS status, COUNT(*) AS count FROM (
                           SELECT status AS display_status FROM cards
                           UNION ALL
                           SELECT CASE WHEN selected=0 THEN 'not_selected' ELSE status END
                           FROM card_proposals
                       ) GROUP BY display_status ORDER BY count DESC"""
                ).fetchall()
            ]
            submission_statuses = [
                dict(row)
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM submissions GROUP BY status ORDER BY count DESC"
                ).fetchall()
            ]
            recurring = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT category, original_text, corrected_text, COUNT(*) AS occurrences,
                           ROUND(AVG(severity), 2) AS avg_severity
                    FROM errors GROUP BY fingerprint HAVING COUNT(*) > 1
                    ORDER BY occurrences DESC, avg_severity DESC LIMIT 10
                    """
                ).fetchall()
            ]
            text_rows = connection.execute(
                "SELECT kind, activity_type, raw_text, transcript FROM submissions WHERE raw_text IS NOT NULL OR transcript IS NOT NULL"
            ).fetchall()

        word_counts = Counter({"writing_words": 0, "journaling_words": 0, "speaking_words": 0})
        for row in text_rows:
            content = row["raw_text"] if row["kind"] == "text" else row["transcript"]
            key = (
                "journaling_words"
                if row["activity_type"] == "journaling"
                else "writing_words"
                if row["kind"] == "text"
                else "speaking_words"
            )
            word_counts[key] += len((content or "").split())
        active_dates = {date.fromisoformat(row["day"]) for row in daily}
        streak = self._current_streak(active_dates)
        return {
            "summary": {**summary, **totals, **word_counts, "current_streak": streak},
            "daily": daily,
            "categories": categories,
            "card_statuses": card_statuses,
            "submission_statuses": submission_statuses,
            "recurring": recurring,
        }

    def proposal_dates(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT local_date FROM card_proposals ORDER BY local_date DESC"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def card_proposals(self, local_date: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM card_proposals WHERE local_date=?
                   ORDER BY CASE category
                       WHEN 'theme_vocabulary' THEN 1 WHEN 'useful_structure' THEN 2
                       WHEN 'grammar_error' THEN 3 ELSE 4 END, position""",
                (local_date,),
            ).fetchall()
        proposals: list[dict[str, Any]] = []
        for row in rows:
            proposal = dict(row)
            proposal["tags"] = parse_json(proposal.pop("tags_json", None), [])
            proposals.append(proposal)
        return proposals

    def save_card_selection(self, local_date: str, selected_ids: set[int]) -> int:
        with self._connect_writable() as connection:
            rows = connection.execute(
                "SELECT id,status FROM card_proposals WHERE local_date=? ORDER BY id", (local_date,)
            ).fetchall()
            valid_ids = {int(row["id"]) for row in rows}
            if not selected_ids <= valid_ids:
                raise ValueError("La sélection contient une carte qui n'appartient pas à ce lot.")
            if not 5 <= len(selected_ids) <= 10:
                raise ValueError("Choisissez entre 5 et 10 cartes.")
            pushed_ids = {int(row["id"]) for row in rows if row["status"] == "pushed"}
            if not pushed_ids <= selected_ids:
                raise ValueError("Une carte déjà envoyée à Anki doit rester sélectionnée.")
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            for row in rows:
                proposal_id = int(row["id"])
                selected = proposal_id in selected_ids
                if row["status"] == "pushed":
                    status = "pushed"
                elif selected and row["status"] == "failed":
                    status = "failed"
                elif selected:
                    status = "pending"
                else:
                    status = "proposed"
                connection.execute(
                    """UPDATE card_proposals SET selected=?,status=?,
                       selected_at=CASE WHEN ?=1 THEN COALESCE(selected_at,?) ELSE NULL END
                       WHERE id=?""",
                    (int(selected), status, int(selected), now, proposal_id),
                )
            connection.commit()
        return len(selected_ids)

    @staticmethod
    def _current_streak(active_dates: set[date]) -> int:
        if not active_dates:
            return 0
        cursor = date.today()
        if cursor not in active_dates:
            cursor -= timedelta(days=1)
        streak = 0
        while cursor in active_dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak
