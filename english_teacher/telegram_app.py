from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from .config import Settings
from .service import EnglishTeacherService
from .utils import chunks, iso_now, safe_extension, sha256_file


LOG = logging.getLogger("english_teacher.telegram")


def input_mode_description(mode: str) -> str:
    return {
        "write_only": "writing only",
        "audio_only": "audio only",
        "both": "writing and audio",
    }[mode]


def input_instructions(mode: str) -> str:
    return {
        "write_only": "Send a written message in English.",
        "audio_only": "Send a voice note or audio file in English.",
        "both": "Send a written message, voice note, or audio file in English.",
    }[mode]


async def is_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user and user.id in settings.allowed_user_ids:
        return True
    if update.effective_message and user:
        await update.effective_message.reply_text(
            f"Access denied. Your Telegram ID is {user.id}; add it explicitly to TELEGRAM_ALLOWED_USER_IDS."
        )
    LOG.warning("Tentative Telegram non autorisée user_id=%s", user.id if user else None)
    return False


def get_service(context: ContextTypes.DEFAULT_TYPE | CallbackContext) -> EnglishTeacherService:
    return context.application.bot_data["service"]


def pending_activities(context: ContextTypes.DEFAULT_TYPE | CallbackContext) -> dict[int, dict[str, Any]]:
    return context.application.bot_data.setdefault("pending_activities", {})


async def send_text(context: ContextTypes.DEFAULT_TYPE | CallbackContext, chat_id: int, text: str) -> None:
    for part in chunks(text):
        await context.bot.send_message(chat_id=chat_id, text=part)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Your English teacher is ready.\n\n"
        f"Active input mode: {input_mode_description(settings.input_mode)}.\n"
        f"{input_instructions(settings.input_mode)} I keep the original production, transcript, "
        "correction, and learning points in the local database.\n\n"
        "Commands: /writing, /speaking, /journaling, /topic, /stats, /retry <id>, /cards, /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Available commands\n\n"
        "/writing — start a writing exercise now\n"
        "/speaking — start a speaking exercise now\n"
        "/journaling — write freely about your day, without a topic\n"
        "/topic — let the teacher choose writing or speaking\n"
        "/stats — show your local learning counters\n"
        "/retry <id> — retry a failed production\n"
        "/cards — generate 20 proposals, or manually retry a failed Anki export\n"
        "/help — show this command summary\n\n"
        f"Active input mode: {input_mode_description(settings.input_mode)}.\n"
        "Automatic tasks follow the schedule and timezone configured in .env."
    )


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await exercise_command(update, context, None)


async def writing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await exercise_command(update, context, "writing")


async def speaking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await exercise_command(update, context, "speaking")


async def journaling_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    chat_id = update.effective_chat.id
    pending_activities(context)[chat_id] = {"topic_id": None, "activity_type": "journaling"}
    await update.effective_message.reply_text(
        "Journaling mode is ready. Write freely in English about your day, your thoughts, or how you feel. "
        "There is no assigned topic. I will save, analyze, and correct your next written message like a normal writing."
    )


async def exercise_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    activity_mode: str | None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
    label = {None: "an exercise", "writing": "a writing exercise", "speaking": "a speaking exercise"}[activity_mode]
    await message.reply_text(f"I am preparing {label} based on your learning history…")
    try:
        topic, _, topic_id = await get_service(context).make_topic(activity_mode)
        await send_text(context, update.effective_chat.id, get_service(context).format_topic(topic))
        selected_mode = activity_mode or str(topic.get("mode") or "writing").strip().lower()
        if selected_mode not in {"writing", "speaking"}:
            selected_mode = "writing"
        pending_activities(context)[update.effective_chat.id] = {
            "topic_id": topic_id,
            "activity_type": selected_mode,
        }
    except Exception:
        LOG.exception("Génération manuelle de l'exercice échouée")
        await message.reply_text("I could not generate the exercise right now. Please check the application logs.")


async def cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    service = get_service(context)
    try:
        created = await service.generate_cards(settings.local_date())
        if created:
            await update.effective_message.reply_text(
                f"I created {created} card proposals: 5 theme vocabulary cards, 5 useful structures, "
                "5 grammar cards, and 5 vocabulary/error cards. Open the Cards page in Streamlit "
                "and select between 5 and 10 cards. Unselected proposals will remain available."
            )
            return
        pushed, remaining = await service.push_pending_cards()
        await update.effective_message.reply_text(
            f"Manual Anki retry complete. Cards sent: {pushed}\nPending: {remaining}. "
            "Normally, validating your selection in Streamlit sends the cards automatically."
        )
    except Exception:
        LOG.exception("Création manuelle des cartes échouée")
        await update.effective_message.reply_text("Card creation failed, but the source data is safely stored.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    stats = await get_service(context).db.stats()
    await update.effective_message.reply_text(
        "Local learning memory\n"
        f"• Productions: {stats['submissions']}\n"
        f"• Archived audio files: {stats['audio']}\n"
        f"• Learning errors: {stats['errors']}\n"
        f"• Synced cards: {stats['cards_pushed']}\n"
        f"• Pending cards: {stats['cards_pending']}"
    )


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /retry <production ID>")
        return
    service = get_service(context)
    submission_id = int(context.args[0])
    submission = await service.db.get_submission(submission_id)
    if not submission or submission["telegram_user_id"] not in settings.allowed_user_ids:
        await update.effective_message.reply_text("Production not found.")
        return
    try:
        production = submission["transcript"] or submission["raw_text"]
        if not production and submission["audio_path"]:
            audio_path = Path(submission["audio_path"])
            if not audio_path.is_absolute():
                audio_path = settings.data_dir / audio_path
            candidates = await service.compare_transcriptions(audio_path, submission["audio_mime_type"])
            await service.db.replace_transcription_candidates(submission_id, candidates)
            succeeded = sum(candidate["status"] == "succeeded" for candidate in candidates.values())
            if not succeeded:
                raise ValueError("all transcription providers failed")
            await service.db.update_submission(
                submission_id,
                status="awaiting_transcript_choice",
                failure_reason=None,
            )
            await update.effective_message.reply_text(
                f"{succeeded} transcript option(s) ready for production {submission_id}. "
                "Open the Journal page in Streamlit and choose the transcript to continue."
            )
            return
        if not production:
            raise ValueError("no content to retry")
        production_type = submission.get("activity_type") or (
            "written text" if submission["kind"] == "text" else "transcribed audio"
        )
        result = await service.correct(submission_id, production, production_type)
        await send_text(context, update.effective_chat.id, service.format_correction(result))
    except Exception as exc:
        LOG.exception("Retraitement %s échoué", submission_id)
        await service.db.update_submission(submission_id, status="failed", failure_reason=str(exc)[:1000])
        await update.effective_message.reply_text(f"Retry failed. Your production is still stored as ID {submission_id}.")


async def process_saved_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    submission_id: int,
    production: str,
    kind: str,
) -> None:
    service = get_service(context)
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        result = await service.correct(submission_id, production, kind)
        await send_text(context, update.effective_chat.id, service.format_correction(result))
    except Exception as exc:
        LOG.exception("Traitement de la production %s échoué", submission_id)
        await service.db.update_submission(submission_id, status="failed", failure_reason=str(exc)[:1000])
        await update.effective_message.reply_text(
            f"Processing failed, but your production is safely stored as ID {submission_id}. "
            f"You can try again with /retry {submission_id}."
        )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
    if settings.input_mode == "audio_only":
        await message.reply_text("The audio_only mode is active. Send a voice note or audio file.")
        return
    text = (message.text or "").strip()
    if not text:
        return
    service = get_service(context)
    chat_id = update.effective_chat.id
    activity = pending_activities(context).get(chat_id)
    if activity and activity["activity_type"] == "speaking":
        await message.reply_text("This is a speaking exercise. Please reply with a voice note or audio file.")
        return
    activity_type = activity["activity_type"] if activity else "writing"
    submission_id = await service.db.add_submission(
        chat_id=chat_id,
        telegram_user_id=update.effective_user.id,
        telegram_message_id=message.message_id,
        kind="text",
        raw_text=text,
        created_at=iso_now(),
        local_date=settings.local_date(),
        topic_id=activity.get("topic_id") if activity else None,
        activity_type=activity_type,
    )
    if submission_id is None:
        return
    pending_activities(context).pop(chat_id, None)
    await message.reply_text(settings.telegram_ack_message)
    production_type = "journal entry" if activity_type == "journaling" else "written text"
    await process_saved_submission(update, context, submission_id, text, production_type)


async def audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
    if settings.input_mode == "write_only":
        await message.reply_text("The write_only mode is active. Send a written message in English.")
        return
    service = get_service(context)
    if await service.db.submission_exists(update.effective_chat.id, message.message_id):
        return

    if message.voice:
        media, kind, original_name, mime_type, default_ext = message.voice, "voice", "telegram-voice.ogg", message.voice.mime_type, ".ogg"
    elif message.audio:
        media, kind, original_name, mime_type, default_ext = message.audio, "audio", message.audio.file_name, message.audio.mime_type, ".mp3"
    elif message.document and (message.document.mime_type or "").startswith("audio/"):
        media, kind, original_name, mime_type, default_ext = message.document, "audio_document", message.document.file_name, message.document.mime_type, ".audio"
    else:
        return

    chat_id = update.effective_chat.id
    activity = pending_activities(context).get(chat_id)
    if activity and activity["activity_type"] in {"writing", "journaling"}:
        expected = "journaling entry" if activity["activity_type"] == "journaling" else "writing exercise"
        await message.reply_text(f"This is a {expected}. Please reply with a written message.")
        return

    max_bytes = settings.telegram_max_audio_mb * 1024 * 1024
    if media.file_size and media.file_size > max_bytes:
        await message.reply_text(f"This audio is too large. The configured limit is {settings.telegram_max_audio_mb} MB.")
        return
    local_now = datetime.now(settings.timezone)
    directory = settings.audio_dir / f"{local_now:%Y}" / f"{local_now:%m}" / f"{local_now:%d}"
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{local_now:%H%M%S}_{message.message_id}_{uuid.uuid4().hex[:8]}{safe_extension(original_name, default_ext)}"
    final_path = directory / filename
    partial_path = final_path.with_suffix(final_path.suffix + ".part")
    try:
        telegram_file = await media.get_file()
        await telegram_file.download_to_drive(custom_path=partial_path)
        partial_path.replace(final_path)
        relative_path = str(final_path.relative_to(settings.data_dir))
        activity_type = activity["activity_type"] if activity else "speaking"
        submission_id = await service.db.add_submission(
            chat_id=chat_id,
            telegram_user_id=update.effective_user.id,
            telegram_message_id=message.message_id,
            kind=kind,
            raw_text=message.caption,
            audio_path=relative_path,
            audio_original_name=original_name,
            audio_mime_type=mime_type,
            audio_sha256=sha256_file(final_path),
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
            created_at=iso_now(),
            local_date=settings.local_date(),
            topic_id=activity.get("topic_id") if activity else None,
            activity_type=activity_type,
        )
        if submission_id is None:
            final_path.unlink(missing_ok=True)
            return
        pending_activities(context).pop(chat_id, None)
        await message.reply_text(
            "Got it — I’m comparing the xAI and ElevenLabs transcripts. "
            "I’ll wait for your choice in Streamlit before correcting it."
        )
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        candidates = await service.compare_transcriptions(final_path, mime_type)
        await service.db.replace_transcription_candidates(submission_id, candidates)
        succeeded = sum(candidate["status"] == "succeeded" for candidate in candidates.values())
        if not succeeded:
            raise ValueError("all transcription providers failed")
        await service.db.update_submission(
            submission_id,
            status="awaiting_transcript_choice",
            failure_reason=None,
        )
        await message.reply_text(
            f"Your audio is archived and {succeeded} transcript option(s) are ready. "
            "Open the Journal page in Streamlit, listen to the audio, and choose the best transcript. "
            "The correction and cards will only be generated after your choice."
        )
    except Exception as exc:
        partial_path.unlink(missing_ok=True)
        LOG.exception("Réception/transcription audio échouée")
        submission = await service.db.get_submission(submission_id) if "submission_id" in locals() and submission_id else None
        if submission:
            await service.db.update_submission(submission_id, status="failed", failure_reason=str(exc)[:1000])
            await message.reply_text(f"The audio is saved, but processing failed (ID {submission_id}). Use /retry {submission_id}.")
        else:
            await message.reply_text("I could not archive this audio. Please check the application logs.")


async def morning_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    service = get_service(context)
    try:
        topic, _, topic_id = await service.make_topic()
        await send_text(context, settings.telegram_chat_id, service.format_topic(topic))
        selected_mode = str(topic.get("mode") or "writing").strip().lower()
        if selected_mode not in {"writing", "speaking"}:
            selected_mode = "writing"
        pending_activities(context)[settings.telegram_chat_id] = {
            "topic_id": topic_id,
            "activity_type": selected_mode,
        }
    except Exception as exc:
        LOG.exception("Tâche matinale échouée")
        await send_text(context, settings.telegram_chat_id, f"Exercise generation failed: {str(exc)[:300]}")


async def evening_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    service = get_service(context)
    try:
        created = await service.generate_cards(settings.local_date())
        pushed, remaining = await service.push_pending_cards()
        await send_text(
            context,
            settings.telegram_chat_id,
            f"Evening review\nCard proposals created: {created}\nSelected cards sent to Anki: {pushed}\nPending: {remaining}",
        )
    except Exception as exc:
        LOG.exception("Tâche du soir échouée")
        await send_text(context, settings.telegram_chat_id, f"Card creation failed: {str(exc)[:300]}")


async def anki_retry_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    try:
        pushed, remaining = await get_service(context).push_pending_cards()
        if pushed:
            await send_text(context, settings.telegram_chat_id, f"Anki retry: {pushed} card(s) sent, {remaining} remaining.")
    except Exception:
        LOG.exception("Nouvel essai Anki échoué")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await is_authorized(update, settings):
        await update.effective_message.reply_text("Unsupported format. Send a written message, voice note, or audio file.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.exception("Erreur Telegram non gérée", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("writing", "Start a writing exercise"),
            BotCommand("speaking", "Start a speaking exercise"),
            BotCommand("journaling", "Write freely about your day"),
            BotCommand("topic", "Let the teacher choose an exercise"),
            BotCommand("cards", "Generate cards or retry Anki export"),
            BotCommand("stats", "Show local learning statistics"),
            BotCommand("retry", "Retry a production by ID"),
            BotCommand("help", "Show all commands"),
        ]
    )


async def post_shutdown(application: Application) -> None:
    service: EnglishTeacherService = application.bot_data["service"]
    await service.close()


def build_application(settings: Settings, service: EnglishTeacherService) -> Application:
    defaults = Defaults(tzinfo=settings.timezone)
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .defaults(defaults)
        .concurrent_updates(4)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if settings.telegram_base_url:
        builder = builder.base_url(settings.telegram_base_url)
    if settings.telegram_base_file_url:
        builder = builder.base_file_url(settings.telegram_base_file_url)
    application = builder.build()
    application.bot_data.update({"settings": settings, "service": service})
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("writing", writing_command))
    application.add_handler(CommandHandler("speaking", speaking_command))
    application.add_handler(CommandHandler("journaling", journaling_command))
    application.add_handler(CommandHandler("topic", topic_command))
    application.add_handler(CommandHandler("cards", cards_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("retry", retry_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, audio_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unknown_message))
    application.add_error_handler(error_handler)

    if application.job_queue is None:
        raise RuntimeError("JobQueue absente; installez python-telegram-bot[job-queue]")
    if settings.morning_enabled:
        application.job_queue.run_daily(
            morning_job,
            time=settings.scheduled_time(settings.morning_clock),
            days=settings.morning_days,
            name="morning-topic",
            job_kwargs={"misfire_grace_time": 3600, "coalesce": True},
        )
    if settings.evening_enabled:
        application.job_queue.run_daily(
            evening_job,
            time=settings.scheduled_time(settings.evening_clock),
            days=settings.evening_days,
            name="evening-cards",
            job_kwargs={"misfire_grace_time": 3600, "coalesce": True},
        )
    if settings.anki_enabled and settings.anki_retry_enabled:
        application.job_queue.run_daily(
            anki_retry_job,
            time=settings.scheduled_time(settings.anki_retry_clock),
            name="anki-retry",
            job_kwargs={"misfire_grace_time": 3600, "coalesce": True},
        )
    return application
