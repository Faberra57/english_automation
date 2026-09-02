from __future__ import annotations

import asyncio
import html
import difflib
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from .config import Settings
from .database import Database
from .dashboard_data import DashboardRepository
from .service import EnglishTeacherService


KIND_LABELS = {
    "text": "Writing",
    "voice": "Note vocale",
    "audio": "Audio",
    "audio_document": "Fichier audio",
}
STATUS_LABELS = {
    "received": "Reçu",
    "awaiting_transcript_choice": "Choix de transcription requis",
    "transcribed": "Transcrit",
    "processed": "Corrigé",
    "failed": "Échec",
}
ACTIVITY_LABELS = {
    "writing": "Writing",
    "speaking": "Speaking",
    "journaling": "Journaling",
}
CARD_CATEGORY_LABELS = {
    "theme_vocabulary": "Vocabulaire lié au thème",
    "useful_structure": "Formulations et structures utiles",
    "grammar_error": "Grammaire et structure",
    "vocabulary_error": "Erreurs de vocabulaire et autres",
}


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root { --ink: #17232b; --muted: #63717a; --accent: #0c7c73; --warm: #f2a65a; }
        .stApp { background: linear-gradient(150deg, #f7fbfa 0%, #f4f1ea 55%, #f9faf8 100%); color: var(--ink); }
        [data-testid="stMain"],
        [data-testid="stMain"] * { color: var(--ink) !important; }
        [data-testid="stMain"] a { color: #086d66 !important; }
        [data-testid="stMain"] [data-testid="stCaptionContainer"] * { color: var(--muted) !important; }
        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea,
        [data-testid="stMain"] [data-baseweb="select"] > div {
            background: white !important; color: var(--ink) !important;
        }
        [data-testid="stMain"] pre,
        [data-testid="stMain"] code { color: #163b42 !important; }
        section[data-testid="stSidebar"],
        section[data-testid="stSidebar"] > div,
        [data-testid="stSidebarContent"],
        [data-testid="stSidebarUserContent"],
        [data-testid="stSidebarNav"] {
            background: #12262c !important;
        }
        [data-testid="stSidebar"] * { color: #f4f7f5; }
        [data-testid="stSidebar"] [data-baseweb="input"],
        [data-testid="stSidebar"] [data-baseweb="select"] > div,
        [data-testid="stSidebar"] [data-baseweb="textarea"] {
            background: #f8fbfa !important;
            border-color: #cbd8d4 !important;
        }
        [data-testid="stSidebar"] [data-baseweb="input"] *,
        [data-testid="stSidebar"] [data-baseweb="select"] > div *,
        [data-testid="stSidebar"] [data-baseweb="textarea"] *,
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea {
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }
        [data-testid="stSidebar"] input::placeholder,
        [data-testid="stSidebar"] textarea::placeholder {
            color: var(--muted) !important;
            -webkit-text-fill-color: var(--muted) !important;
            opacity: 1;
        }
        [data-testid="stMetric"] {
            background: rgba(255,255,255,.82); border: 1px solid rgba(18,38,44,.08);
            padding: 1rem; border-radius: 18px; box-shadow: 0 8px 26px rgba(18,38,44,.05);
        }
        [data-testid="stMetric"] * { color: var(--ink) !important; }
        [data-testid="stMetricLabel"] { opacity: .72; }
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] summary * { color: var(--ink) !important; }
        .hero {
            padding: 1.55rem 1.7rem; border-radius: 24px; color: white; margin-bottom: 1.2rem;
            background: radial-gradient(circle at 90% 20%, rgba(242,166,90,.9), transparent 28%),
                        linear-gradient(120deg, #12343b, #0c7c73);
            box-shadow: 0 18px 48px rgba(12,71,70,.17);
        }
        .hero h1 { margin: 0; font-size: clamp(1.9rem, 4vw, 3rem); letter-spacing: -.04em; }
        .hero p { margin: .5rem 0 0; opacity: .84; max-width: 760px; }
        [data-testid="stMain"] .hero,
        [data-testid="stMain"] .hero * { color: white !important; }
        .day-title { color: var(--ink); font-size: 1.35rem; font-weight: 750; margin: 1.4rem 0 .65rem; }
        .topic-card {
            background: #e4f2ee; border-left: 5px solid var(--accent); color: var(--ink);
            padding: .9rem 1.1rem; border-radius: 6px 16px 16px 6px; margin-bottom: .8rem;
        }
        .topic-card small { color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
        .pill { display: inline-block; padding: .18rem .55rem; margin-right: .3rem; border-radius: 999px;
                background: #e9efed; color: #294048 !important; font-size: .78rem; font-weight: 650; }
        .pill.ok { background: #d9f2e7; color: #176344 !important; }
        .pill.warn { background: #fff0da; color: #8a5419 !important; }
        .muted { color: var(--muted) !important; }
        .correction { border-left: 4px solid var(--warm); padding-left: 1rem; }
        .annotation-legend { display: flex; gap: 1rem; margin: .35rem 0 .65rem; font-size: .82rem; }
        [data-testid="stMain"] .legend-error,
        [data-testid="stMain"] .inline-error {
            color: #b42318 !important;
        }
        .inline-error {
            text-decoration: line-through;
            text-decoration-thickness: 2px;
            text-decoration-color: #d92d20;
            background: #fee4e2;
            border-radius: 4px;
            padding: .05rem .16rem;
        }
        [data-testid="stMain"] .legend-fix,
        [data-testid="stMain"] .inline-fix {
            color: #137333 !important;
        }
        .inline-fix {
            text-decoration: none;
            background: #dcfce7;
            border-bottom: 2px solid #22a06b;
            border-radius: 4px;
            padding: .05rem .16rem;
            font-weight: 700;
        }
        .annotated-text {
            background: white;
            border: 1px solid #dce6e2;
            border-radius: 14px;
            padding: 1rem 1.1rem;
            line-height: 1.85;
            margin-bottom: 1rem;
        }
        .advanced-rewrite {
            background: linear-gradient(135deg, #e7f3ff, #eef8f4);
            border: 1px solid #b9d8d1;
            border-radius: 14px;
            padding: 1rem 1.1rem;
            line-height: 1.65;
        }
        div[data-testid="stExpander"] { background: rgba(255,255,255,.76); border-radius: 16px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=15, show_spinner=False)
def _date_bounds(database_path: str) -> tuple[date, date]:
    return DashboardRepository(Path(database_path)).date_bounds()


@st.cache_data(ttl=15, show_spinner=False)
def _journal_data(
    database_path: str,
    start_date: date,
    end_date: date,
    kinds: tuple[str, ...],
    activity_types: tuple[str, ...],
    statuses: tuple[str, ...],
    search: str,
    limit: int,
) -> dict[str, Any]:
    return DashboardRepository(Path(database_path)).journal(
        start_date=start_date,
        end_date=end_date,
        kinds=kinds,
        activity_types=activity_types,
        statuses=statuses,
        search=search,
        limit=limit,
    )


@st.cache_data(ttl=15, show_spinner=False)
def _statistics_data(database_path: str) -> dict[str, Any]:
    return DashboardRepository(Path(database_path)).statistics()


@st.cache_data(ttl=15, show_spinner=False)
def _proposal_dates(database_path: str) -> list[str]:
    return DashboardRepository(Path(database_path)).proposal_dates()


@st.cache_data(ttl=15, show_spinner=False)
def _card_proposals(database_path: str, local_date: str) -> list[dict[str, Any]]:
    return DashboardRepository(Path(database_path)).card_proposals(local_date)


def _hero(title: str, subtitle: str) -> None:
    st.markdown(
        f'<section class="hero"><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></section>',
        unsafe_allow_html=True,
    )


def _sidebar_header(settings: Settings) -> None:
    st.sidebar.markdown("## English Studio")
    st.sidebar.caption(f"Mémoire locale · {settings.database_path}")
    if st.sidebar.button("Actualiser les données", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


def render_journal(settings: Settings) -> None:
    inject_styles()
    _sidebar_header(settings)
    _hero(
        "Ton journal d’anglais",
        "Retrouve chaque production, son thème, la correction du professeur et les cartes générées — sans perdre le fil.",
    )
    try:
        first_date, last_date = _date_bounds(str(settings.database_path))
    except (FileNotFoundError, OSError) as exc:
        st.error(str(exc))
        st.info("Lance d’abord le bot ou la commande de vérification pour initialiser la base SQLite.")
        return

    st.sidebar.markdown("### Filtres du journal")
    default_start = max(first_date, last_date - timedelta(days=30))
    period = st.sidebar.date_input(
        "Période",
        value=(default_start, last_date),
        min_value=first_date,
        max_value=max(last_date, date.today()),
    )
    if isinstance(period, tuple) and len(period) == 2:
        start_date, end_date = period
    else:
        start_date = end_date = period if isinstance(period, date) else last_date
    kind_options = list(KIND_LABELS)
    kinds = st.sidebar.multiselect(
        "Formats",
        kind_options,
        default=kind_options,
        format_func=lambda item: KIND_LABELS[item],
    )
    activity_options = list(ACTIVITY_LABELS)
    activity_types = st.sidebar.multiselect(
        "Types d’activité",
        activity_options,
        default=activity_options,
        format_func=lambda item: ACTIVITY_LABELS[item],
    )
    status_options = list(STATUS_LABELS)
    statuses = st.sidebar.multiselect(
        "États",
        status_options,
        default=status_options,
        format_func=lambda item: STATUS_LABELS[item],
    )
    search = st.sidebar.text_input("Rechercher", placeholder="mot, thème ou correction…")
    limit = st.sidebar.select_slider("Nombre maximum", options=[25, 50, 100, 200, 500], value=100)

    result = _journal_data(
        str(settings.database_path),
        start_date,
        end_date,
        tuple(kinds),
        tuple(activity_types),
        tuple(statuses),
        search,
        limit,
    )
    submissions = result["submissions"]
    journal_count = sum(item.get("activity_type") == "journaling" for item in submissions)
    writing_count = sum(
        item["kind"] == "text" and item.get("activity_type") != "journaling" for item in submissions
    )
    audio_count = sum(item["kind"] != "text" for item in submissions)
    error_count = sum(int(item["error_count"]) for item in submissions)
    cols = st.columns(5)
    cols[0].metric("Productions", len(submissions))
    cols[1].metric("Writings", writing_count)
    cols[2].metric("Journaling", journal_count)
    cols[3].metric("Audios", audio_count)
    cols[4].metric("Erreurs travaillées", error_count)

    if not submissions:
        st.info("Aucune production ne correspond à ces filtres.")
        return
    if result["truncated"]:
        st.warning("La liste atteint la limite choisie. Réduis la période ou augmente la limite pour tout afficher.")

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for submission in submissions:
        by_day[submission["local_date"]].append(submission)
    for day, day_submissions in by_day.items():
        formatted_day = date.fromisoformat(day).strftime("%d/%m/%Y")
        st.markdown(
            f'<div class="day-title">{formatted_day} · {len(day_submissions)} production(s)</div>',
            unsafe_allow_html=True,
        )
        for submission in day_submissions:
            _render_submission(settings, submission)


def _render_topic(topic: dict[str, Any]) -> None:
    if not topic:
        return
    title = html.escape(str(topic.get("title") or "Sujet du jour"))
    mode = html.escape(str(topic.get("mode") or "thème"))
    prompt = html.escape(str(topic.get("prompt") or ""))
    st.markdown(
        f'<div class="topic-card"><small>{mode}</small><br><strong>{title}</strong><br>{prompt}</div>',
        unsafe_allow_html=True,
    )


def _render_submission(settings: Settings, submission: dict[str, Any]) -> None:
    activity_type = submission.get("activity_type") or (
        "writing" if submission["kind"] == "text" else "speaking"
    )
    kind = ACTIVITY_LABELS.get(activity_type, KIND_LABELS.get(submission["kind"], submission["kind"]))
    created = _format_time(submission.get("created_at"))
    status = STATUS_LABELS.get(submission["status"], submission["status"])
    title = f"{created} · {kind} · {status} · {submission['error_count']} erreur(s)"
    with st.expander(title, expanded=False):
        if activity_type == "journaling":
            st.markdown(
                '<div class="topic-card"><small>journaling</small><br><strong>Réflexion libre</strong>'
                '<br>Aucun sujet imposé pour cette entrée.</div>',
                unsafe_allow_html=True,
            )
        else:
            _render_topic(submission.get("topic") or {})
        st.markdown(
            f'<span class="pill">{html.escape(kind)}</span>'
            f'<span class="pill ok">{html.escape(status)}</span>'
            f'<span class="pill warn">{submission["card_count"]} carte(s)</span>',
            unsafe_allow_html=True,
        )
        audio_path = _audio_path(settings, submission.get("audio_path"))
        if audio_path:
            if audio_path.is_file():
                st.audio(str(audio_path), format=submission.get("audio_mime_type") or "audio/ogg")
                st.caption(submission.get("audio_original_name") or audio_path.name)
            else:
                st.warning(f"Fichier audio introuvable : {audio_path}")
        original_tab, correction_tab, learning_tab, details_tab = st.tabs(
            ["Production", "Correction IA", "Erreurs & cartes", "Détails"]
        )
        with original_tab:
            raw_text = str(submission.get("raw_text") or "").strip()
            transcript = str(submission.get("transcript") or "").strip()
            if raw_text:
                st.markdown("**Texte original**" if submission["kind"] == "text" else "**Légende**")
                st.write(raw_text)
            if transcript:
                st.markdown("**Transcription archivée**")
                st.write(transcript)
            if not raw_text and not transcript:
                st.caption("Aucun texte archivé pour cette production.")
            candidates = submission.get("transcription_candidates") or []
            if candidates:
                st.markdown("### Comparaison des transcriptions")
                st.caption(
                    "Écoutez l’audio, comparez les deux versions, puis gardez celle qui reproduit "
                    "le plus fidèlement vos mots et vos erreurs."
                )
                _render_transcription_candidates(settings, submission, candidates)
        with correction_tab:
            _render_correction(submission)
        with learning_tab:
            _render_errors(submission.get("errors") or [])
        with details_tab:
            st.caption(f"Production #{submission['id']} · message Telegram #{submission['telegram_message_id']}")
            st.write(f"État technique : `{submission['status']}`")
            if submission.get("processed_at"):
                st.write(f"Traitée le : {_format_datetime(submission['processed_at'])}")
            if submission.get("failure_reason"):
                st.error(submission["failure_reason"])
            st.divider()
            st.markdown("#### Zone dangereuse")
            st.caption(
                "Cette action supprime définitivement la production, son fichier audio, ses deux "
                "transcriptions, son choix, sa correction, ses erreurs et ses cartes directement liées."
            )
            confirmed = st.checkbox(
                f"Je confirme la suppression définitive de la production #{submission['id']}",
                key=f"confirm-delete-submission-{submission['id']}",
            )
            if st.button(
                "Supprimer définitivement cette production",
                key=f"delete-submission-{submission['id']}",
                disabled=not confirmed,
                use_container_width=True,
            ):
                repository = DashboardRepository(settings.database_path)
                try:
                    repository.delete_submission(
                        int(submission["id"]),
                        data_dir=settings.data_dir,
                        audio_dir=settings.audio_dir,
                    )
                    st.cache_data.clear()
                    st.toast(f"Production #{submission['id']} supprimée définitivement.", icon="🗑️")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Suppression impossible : {exc}")


def _render_transcription_candidates(
    settings: Settings, submission: dict[str, Any], candidates: list[dict[str, Any]]
) -> None:
    provider_labels = {"xai": "xAI (Grok)", "elevenlabs": "ElevenLabs Scribe"}
    columns = st.columns(max(1, len(candidates)))
    already_selected = next((item for item in candidates if item.get("selected")), None)
    for column, candidate in zip(columns, candidates):
        provider = str(candidate["provider"])
        label = provider_labels.get(provider, provider)
        with column:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                latency = candidate.get("latency_ms")
                if latency is not None:
                    st.caption(f"Temps de transcription : {float(latency) / 1000:.1f} s")
                if candidate["status"] == "failed":
                    st.error("La transcription a échoué pour ce fournisseur.")
                    if candidate.get("error"):
                        st.caption(str(candidate["error"])[:300])
                    continue
                st.write(str(candidate.get("transcript") or ""))
                if candidate.get("selected"):
                    st.success("Transcription conservée")
                elif not already_selected and submission["status"] == "awaiting_transcript_choice":
                    if st.button(
                        f"Garder {label}",
                        key=f"choose-transcript-{submission['id']}-{candidate['id']}",
                        use_container_width=True,
                        type="primary",
                    ):
                        repository = DashboardRepository(settings.database_path)
                        try:
                            with st.spinner("Correction DeepSeek et génération des cartes en cours…"):
                                selection = repository.choose_transcription(
                                    int(submission["id"]), int(candidate["id"])
                                )
                                card_count, card_error = asyncio.run(
                                    _process_transcript_choice(settings, selection)
                                )
                            st.cache_data.clear()
                            if card_error:
                                st.warning(
                                    "La correction est terminée, mais la génération des cartes a échoué : "
                                    + card_error
                                )
                            else:
                                st.success(
                                    f"{label} conservé. Correction terminée et {card_count} carte(s) proposée(s)."
                                )
                            st.rerun()
                        except Exception as exc:
                            st.cache_data.clear()
                            st.error(f"Le choix est enregistré, mais le traitement a échoué : {exc}")


async def _process_transcript_choice(
    settings: Settings, selection: dict[str, Any]
) -> tuple[int, str | None]:
    database = Database(settings)
    service = EnglishTeacherService(settings, database)
    try:
        try:
            await service.correct(
                int(selection["submission_id"]),
                str(selection["transcript"]),
                str(selection.get("activity_type") or "transcribed audio"),
            )
        except Exception as exc:
            await database.update_submission(
                int(selection["submission_id"]), status="failed", failure_reason=str(exc)[:1000]
            )
            raise
        try:
            created = await service.generate_cards(
                str(selection["local_date"]),
                source_submission_id=int(selection["submission_id"]),
            )
            return created, None
        except Exception as exc:
            return 0, str(exc)[:500]
    finally:
        await service.close()


def _render_correction(submission: dict[str, Any]) -> None:
    correction = submission.get("correction") or {}
    if not correction:
        fallback = str(submission.get("correction_text") or "").strip()
        st.write(fallback or "La correction n’est pas encore disponible.")
        return
    feedback = str(correction.get("feedback") or "").strip()
    if feedback:
        st.markdown('<div class="correction">', unsafe_allow_html=True)
        st.write(feedback)
        st.markdown("</div>", unsafe_allow_html=True)
    source_value = submission.get("raw_text") if submission.get("kind") == "text" else submission.get("transcript")
    source_text = str(source_value or "").strip()
    errors = submission.get("errors") or []
    if source_text:
        st.markdown("**Your text with corrections**")
        st.markdown(
            '<div class="annotation-legend">'
            '<span class="legend-error">Struck through = error</span>'
            '<span class="legend-fix">Green = correction</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        corrected_for_diff = str(correction.get("corrected_version") or "").strip()
        annotated = (
            annotate_text_diff(source_text, corrected_for_diff)
            if corrected_for_diff
            else annotate_text(source_text, errors)
        )
        st.markdown(annotated, unsafe_allow_html=True)
    strengths = correction.get("strengths") or []
    if strengths:
        st.markdown("**Points forts**")
        for strength in strengths:
            st.markdown(f"- {strength}")
    corrected = str(correction.get("corrected_version") or "").strip()
    if corrected:
        st.markdown("**Clean corrected version**")
        st.success(corrected)
    advanced = str(correction.get("advanced_rewrite") or "").strip()
    if advanced:
        st.markdown("**C1–C2 reformulation**")
        st.markdown(
            f'<div class="advanced-rewrite">{_escape_with_breaks(advanced)}</div>',
            unsafe_allow_html=True,
        )
    elif correction:
        st.caption("The C1–C2 reformulation will be available for newly processed or retried productions.")


def _render_errors(errors: list[dict[str, Any]]) -> None:
    if not errors:
        st.success("Aucune erreur notable détectée pour cette production.")
        return
    for error in errors:
        severity = "●" * int(error.get("severity") or 0) + "○" * (5 - int(error.get("severity") or 0))
        st.markdown(f"**{error['rank']}. {error['category']}** · gravité {severity}")
        st.markdown(f"~~{error['original_text']}~~  →  **{error['corrected_text']}**")
        if error.get("explanation_fr"):
            st.write(error["explanation_fr"])
        if error.get("practice_tip"):
            st.caption(f"Conseil : {error['practice_tip']}")
        if error.get("card_id"):
            status = error.get("card_status") or "pending"
            with st.container(border=True):
                st.markdown(f"**Carte Anki · {status}**")
                st.write(f"**Recto :** {error.get('card_front', '')}")
                st.write(f"**Verso :** {error.get('card_back', '')}")
                tags = error.get("card_tags") or []
                if tags:
                    st.caption("Tags : " + ", ".join(map(str, tags)))
        st.divider()


def render_statistics(settings: Settings) -> None:
    inject_styles()
    _sidebar_header(settings)
    _hero(
        "Progression & statistiques",
        "Une page dédiée pour comprendre ton rythme, tes erreurs récurrentes et l’évolution de ta pratique.",
    )
    try:
        data = _statistics_data(str(settings.database_path))
    except (FileNotFoundError, OSError) as exc:
        st.error(str(exc))
        return
    summary = data["summary"]
    processed = int(summary.get("processed") or 0)
    flawless = int(summary.get("flawless") or 0)
    flawless_rate = round(100 * flawless / processed) if processed else 0
    error_average = round(int(summary.get("errors") or 0) / processed, 1) if processed else 0

    first_row = st.columns(4)
    first_row[0].metric("Jours actifs", int(summary.get("active_days") or 0))
    first_row[1].metric("Série actuelle", f"{summary['current_streak']} jour(s)")
    first_row[2].metric("Productions corrigées", processed)
    first_row[3].metric("Sans erreur notable", f"{flawless_rate} %")
    second_row = st.columns(5)
    second_row[0].metric("Mots écrits", int(summary.get("writing_words") or 0))
    second_row[1].metric("Mots de journal", int(summary.get("journaling_words") or 0))
    second_row[2].metric("Mots prononcés", int(summary.get("speaking_words") or 0))
    second_row[3].metric("Erreurs / production", error_average)
    second_row[4].metric("Cartes Anki", int(summary.get("cards") or 0))

    st.markdown("### Comparatif des transcriptions")
    provider_choices = pd.DataFrame(data["transcription_provider_choices"])
    if provider_choices.empty:
        st.info("Aucun choix de transcription enregistré pour le moment.")
    else:
        labels = {"xai": "xAI (Grok)", "elevenlabs": "ElevenLabs Scribe"}
        provider_choices["provider"] = provider_choices["provider"].map(
            lambda value: labels.get(value, value)
        )
        winner = provider_choices.iloc[0]
        comparison_cols = st.columns(3)
        comparison_cols[0].metric("Choix enregistrés", int(provider_choices["count"].sum()))
        comparison_cols[1].metric("API la plus choisie", str(winner["provider"]))
        comparison_cols[2].metric("Part du meilleur choix", f"{float(winner['percentage']):.1f} %")
        st.bar_chart(provider_choices.set_index("provider")["count"], color="#0c7c73")
        choice_daily = pd.DataFrame(data["transcription_choices_daily"])
        if not choice_daily.empty:
            choice_daily["day"] = pd.to_datetime(choice_daily["day"])
            choice_daily = choice_daily.set_index("day").rename(
                columns={"xai": "xAI (Grok)", "elevenlabs": "ElevenLabs Scribe"}
            )
            with st.expander("Voir l’évolution des choix dans le temps"):
                st.line_chart(choice_daily, color=["#0c7c73", "#9d6ab3"])

    st.markdown("### Activité dans le temps")
    daily = pd.DataFrame(data["daily"])
    if not daily.empty:
        daily["day"] = pd.to_datetime(daily["day"])
        daily = daily.set_index("day")
        st.line_chart(daily[["submissions", "errors"]], color=["#0c7c73", "#f2a65a"])
        with st.expander("Voir le détail writing / speaking"):
            st.bar_chart(
                daily[["writings", "journal_entries", "audios"]],
                color=["#2f6f8f", "#0c7c73", "#9d6ab3"],
            )
    else:
        st.info("Pas encore assez de données pour afficher une tendance.")

    left, right = st.columns(2)
    with left:
        st.markdown("### Erreurs les plus fréquentes")
        categories = pd.DataFrame(data["categories"])
        if not categories.empty:
            st.bar_chart(categories.set_index("category")["occurrences"], color="#f2a65a")
            st.dataframe(
                categories.rename(
                    columns={"category": "Catégorie", "occurrences": "Occurrences", "avg_severity": "Gravité moy."}
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("Aucune erreur mémorisée.")
    with right:
        st.markdown("### État des cartes Anki")
        cards = pd.DataFrame(data["card_statuses"])
        if not cards.empty:
            st.bar_chart(cards.set_index("status")["count"], color="#0c7c73")
        else:
            st.info("Aucune carte générée.")
        st.markdown("### Santé des traitements")
        statuses = pd.DataFrame(data["submission_statuses"])
        if not statuses.empty:
            statuses["status"] = statuses["status"].map(lambda value: STATUS_LABELS.get(value, value))
            st.bar_chart(statuses.set_index("status")["count"], color="#2f6f8f")

    st.markdown("### Points à retravailler")
    recurring = pd.DataFrame(data["recurring"])
    if recurring.empty:
        st.success("Aucun motif d’erreur répété n’a encore été détecté.")
    else:
        st.dataframe(
            recurring.rename(
                columns={
                    "category": "Catégorie",
                    "original_text": "Forme initiale",
                    "corrected_text": "Correction",
                    "occurrences": "Répétitions",
                    "avg_severity": "Gravité moy.",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )


def render_cards(settings: Settings) -> None:
    inject_styles()
    _sidebar_header(settings)
    _hero(
        "Sélection des cartes",
        "Compare les 20 propositions, choisis-en 5 à 10 puis envoie-les directement vers Anki.",
    )
    notice = st.session_state.pop("card_push_notice", None)
    if notice:
        level, message = notice
        getattr(st, level)(message)
    try:
        dates = _proposal_dates(str(settings.database_path))
    except (FileNotFoundError, OSError) as exc:
        st.error(str(exc))
        return
    if not dates:
        st.info("Aucun lot n’a encore été généré. Utilise /cards dans Telegram après une production corrigée.")
        return
    selected_date = st.sidebar.selectbox(
        "Lot de cartes",
        dates,
        format_func=lambda value: date.fromisoformat(value).strftime("%d/%m/%Y"),
    )
    proposals = _card_proposals(str(settings.database_path), selected_date)
    selected_count = sum(bool(card["selected"]) for card in proposals)
    pushed_count = sum(card["status"] == "pushed" for card in proposals)
    failed_count = sum(card["status"] == "failed" for card in proposals)
    metrics = st.columns(4)
    metrics[0].metric("Propositions", len(proposals))
    metrics[1].metric("Sélectionnées", selected_count)
    metrics[2].metric("Dans Anki", pushed_count)
    metrics[3].metric("À vérifier", failed_count)
    if settings.anki_enabled:
        st.info(
            "La validation envoie immédiatement les cartes sélectionnées à Anki, puis lance la "
            "synchronisation AnkiWeb. /cards n’est utile que pour générer un lot ou relancer un échec."
        )
    else:
        st.warning(
            "Anki est actuellement désactivé. La sélection sera conservée et mise en attente jusqu’à ce que "
            "ANKI_ENABLED=true."
        )

    selected_ids: set[int] = set()
    with st.form(f"card-selection-{selected_date}"):
        for category, label in CARD_CATEGORY_LABELS.items():
            st.markdown(f"### {label}")
            category_cards = [card for card in proposals if card["category"] == category]
            columns = st.columns(2)
            for index, card in enumerate(category_cards):
                with columns[index % 2]:
                    with st.container(border=True):
                        pushed = card["status"] == "pushed"
                        checked = st.checkbox(
                            f"Sélectionner · carte {card['position']}",
                            value=bool(card["selected"]),
                            disabled=pushed,
                            key=f"proposal-{card['id']}",
                        )
                        if checked or pushed:
                            selected_ids.add(int(card["id"]))
                        status_label = {
                            "proposed": "Non choisie",
                            "pending": "Sélectionnée · en attente",
                            "pushed": "Envoyée à Anki",
                            "failed": "Sélectionnée · échec Anki",
                        }.get(card["status"], card["status"])
                        st.caption(status_label)
                        st.markdown("**Recto**")
                        st.write(card["front"])
                        st.markdown("**Verso**")
                        st.write(card["back"])
                        if card.get("rationale"):
                            st.caption(f"Pourquoi cette carte : {card['rationale']}")
                        if card.get("tags"):
                            st.caption("Tags : " + ", ".join(map(str, card["tags"])))
        st.markdown(f"**Sélection actuelle : {len(selected_ids)} / 5–10 cartes**")
        submitted = st.form_submit_button("Valider et envoyer vers Anki", type="primary", use_container_width=True)
    if submitted:
        try:
            count = DashboardRepository(settings.database_path).save_card_selection(selected_date, selected_ids)
        except ValueError as exc:
            st.error(str(exc))
        else:
            result = asyncio.run(_push_selected_cards(settings, selected_date))
            pushed = int(result["pushed"])
            remaining = int(result["remaining"])
            if not result["anki_enabled"]:
                notice = (
                    "warning",
                    f"Sélection enregistrée : {count} carte(s). Anki est désactivé : "
                    "elles restent en attente et pourront être renvoyées plus tard.",
                )
            elif result["sync_error"]:
                notice = (
                    "warning",
                    f"{pushed} carte(s) envoyée(s) à Anki, {remaining} restante(s). "
                    f"Synchronisation AnkiWeb non terminée : {result['sync_error']}",
                )
            elif result["sync_attempted"] and result["sync_succeeded"]:
                notice = (
                    "success",
                    f"{pushed} carte(s) envoyée(s) à Anki et synchronisation AnkiWeb terminée. "
                    "Les propositions non choisies restent visibles.",
                )
            elif pushed:
                notice = ("success", f"{pushed} carte(s) envoyée(s) à Anki.")
            else:
                notice = (
                    "info",
                    "Sélection enregistrée. Aucune nouvelle carte à envoyer : les cartes choisies "
                    "avaient déjà été ajoutées à Anki.",
                )
            st.session_state["card_push_notice"] = notice
            st.cache_data.clear()
            st.rerun()


async def _push_selected_cards(settings: Settings, local_date: str) -> dict[str, Any]:
    database = Database(settings)
    service = EnglishTeacherService(settings, database)
    try:
        return await service.push_pending_cards_detailed(local_date=local_date)
    finally:
        await service.close()


def _audio_path(settings: Settings, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    path = Path(stored_path)
    return path if path.is_absolute() else settings.data_dir / path


def _format_time(raw_value: str | None) -> str:
    try:
        return datetime.fromisoformat(str(raw_value)).strftime("%H:%M")
    except ValueError:
        return "--:--"


def _format_datetime(raw_value: str | None) -> str:
    try:
        return datetime.fromisoformat(str(raw_value)).strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return str(raw_value or "—")


def annotate_text(text: str, errors: list[dict[str, Any]]) -> str:
    """Return safe HTML with non-overlapping inline error replacements."""
    matches: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    lowered = text.lower()
    for error in errors:
        original = str(error.get("original_text") or "").strip()
        corrected = str(error.get("corrected_text") or "").strip()
        if not original or not corrected:
            continue
        search_from = 0
        while search_from < len(text):
            start = text.find(original, search_from)
            if start < 0:
                start = lowered.find(original.lower(), search_from)
            if start < 0:
                break
            end = start + len(original)
            if not any(start < used_end and end > used_start for used_start, used_end in occupied):
                matches.append((start, end, corrected))
                occupied.append((start, end))
                break
            search_from = end
    matches.sort(key=lambda item: item[0])
    rendered: list[str] = []
    cursor = 0
    for start, end, corrected in matches:
        rendered.append(_escape_with_breaks(text[cursor:start]))
        rendered.append(f'<span class="inline-error">{_escape_with_breaks(text[start:end])}</span>')
        rendered.append(' <span aria-hidden="true">→</span> ')
        rendered.append(f'<ins class="inline-fix">{_escape_with_breaks(corrected)}</ins>')
        cursor = end
    rendered.append(_escape_with_breaks(text[cursor:]))
    return '<div class="annotated-text">' + "".join(rendered) + "</div>"


def annotate_text_diff(original: str, corrected: str) -> str:
    """Annotate every textual change between the production and corrected version."""
    token_pattern = re.compile(r"\s+|[\w’'-]+|[^\w\s]", re.UNICODE)
    original_tokens = token_pattern.findall(original)
    corrected_tokens = token_pattern.findall(corrected)
    matcher = difflib.SequenceMatcher(a=original_tokens, b=corrected_tokens, autojunk=False)
    rendered: list[str] = []
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        original_chunk = "".join(original_tokens[a_start:a_end])
        corrected_chunk = "".join(corrected_tokens[b_start:b_end])
        if opcode == "equal":
            rendered.append(_escape_with_breaks(original_chunk))
        elif opcode == "replace":
            rendered.append(f'<span class="inline-error">{_escape_with_breaks(original_chunk)}</span>')
            rendered.append(' <span aria-hidden="true">→</span> ')
            rendered.append(f'<ins class="inline-fix">{_escape_with_breaks(corrected_chunk)}</ins>')
        elif opcode == "delete":
            rendered.append(f'<span class="inline-error">{_escape_with_breaks(original_chunk)}</span>')
        elif opcode == "insert":
            rendered.append(f'<ins class="inline-fix">{_escape_with_breaks(corrected_chunk)}</ins>')
    return '<div class="annotated-text">' + "".join(rendered) + "</div>"


def _escape_with_breaks(value: str) -> str:
    return html.escape(value).replace("\n", "<br>")
