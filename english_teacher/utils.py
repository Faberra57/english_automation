from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


TOKEN_RE = re.compile(r"[a-z][a-z'-]{1,}", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "")}


def clean_json_text(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    return candidate.strip()


def bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def chunks(text: str, limit: int = 3900) -> Iterable[str]:
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        yield remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip()


def safe_extension(filename: str | None, default: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

