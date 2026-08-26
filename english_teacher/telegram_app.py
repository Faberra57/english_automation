from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

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


async def is_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user and user.id in settings.allowed_user_ids:
        return True
    if update.effective_message and user:
        await update.effective_message.reply_text(
            f"Accès refusé. Ton ID Telegram est {user.id}; ajoute-le explicitement à TELEGRAM_ALLOWED_USER_IDS."
        )
    LOG.warning("Tentative Telegram non autorisée user_id=%s", user.id if user else None)
    return False


def get_service(context: ContextTypes.DEFAULT_TYPE | CallbackContext) -> EnglishTeacherService:
    return context.application.bot_data["service"]


async def send_text(context: ContextTypes.DEFAULT_TYPE | CallbackContext, chat_id: int, text: str) -> None:
    for part in chunks(text):
        await context.bot.send_message(chat_id=chat_id, text=part)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Ton professeur d'anglais est prêt.\n\n"
        "Envoie un texte, une note vocale ou un fichier audio. Je conserve la production originale, "
        "la transcription, la correction et tes erreurs dans la base locale.\n\n"
        "Commandes : /topic, /cards, /stats, /retry <id>, /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    await update.effective_message.reply_text(
        "/topic — générer immédiatement un sujet ciblé\n"
        "/cards — créer et envoyer les fiches du jour vers Anki\n"
        "/stats — voir les compteurs locaux\n"
        "/retry <id> — retraiter une production en échec\n\n"
        "Les tâches automatiques suivent les horaires et le fuseau définis dans .env."
    )


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
    await message.reply_text("Je prépare un sujet à partir de tes faiblesses…")
    try:
        topic, _ = await get_service(context).make_topic()
        await send_text(context, update.effective_chat.id, get_service(context).format_topic(topic))
    except Exception:
        LOG.exception("Génération manuelle du sujet échouée")
        await message.reply_text("Impossible de générer le sujet pour le moment. Consulte les logs du conteneur.")


async def cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    service = get_service(context)
    try:
        created = await service.generate_cards(settings.local_date())
        pushed, remaining = await service.push_pending_cards()
        await update.effective_message.reply_text(
            f"Fiches créées : {created}\nEnvoyées vers Anki : {pushed}\nEn attente : {remaining}"
        )
    except Exception:
        LOG.exception("Création manuelle des cartes échouée")
        await update.effective_message.reply_text("La création des fiches a échoué; les données sources sont conservées.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    stats = await get_service(context).db.stats()
    await update.effective_message.reply_text(
        "Mémoire locale\n"
        f"• Productions : {stats['submissions']}\n"
        f"• Audios archivés : {stats['audio']}\n"
        f"• Erreurs mémorisées : {stats['errors']}\n"
        f"• Fiches synchronisées : {stats['cards_pushed']}\n"
        f"• Fiches en attente : {stats['cards_pending']}"
    )


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage : /retry <id de production>")
        return
    service = get_service(context)
    submission_id = int(context.args[0])
    submission = await service.db.get_submission(submission_id)
    if not submission or submission["telegram_user_id"] not in settings.allowed_user_ids:
        await update.effective_message.reply_text("Production introuvable.")
        return
    try:
        production = submission["transcript"] or submission["raw_text"]
        if not production and submission["audio_path"]:
            audio_path = Path(submission["audio_path"])
            if not audio_path.is_absolute():
                audio_path = settings.data_dir / audio_path
            transcription = await service.groq.transcribe(audio_path, submission["audio_mime_type"])
            production = transcription["text"].strip()
            await service.db.update_submission(
                submission_id, transcript=production, transcription_json=json.dumps(transcription, ensure_ascii=False)
            )
        if not production:
            raise ValueError("aucun contenu à retraiter")
        result = await service.correct(submission_id, production, submission["kind"])
        await send_text(context, update.effective_chat.id, service.format_correction(result))
    except Exception as exc:
        LOG.exception("Retraitement %s échoué", submission_id)
        await service.db.update_submission(submission_id, status="failed", failure_reason=str(exc)[:1000])
        await update.effective_message.reply_text(f"Retraitement impossible; ID conservé : {submission_id}")


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
            f"Le traitement a échoué, mais ta production est bien sauvegardée (ID {submission_id}). "
            f"Tu pourras lancer /retry {submission_id}."
        )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return
    service = get_service(context)
    submission_id = await service.db.add_submission(
        chat_id=update.effective_chat.id,
        telegram_user_id=update.effective_user.id,
        telegram_message_id=message.message_id,
        kind="text",
        raw_text=text,
        created_at=iso_now(),
        local_date=settings.local_date(),
    )
    if submission_id is None:
        return
    await message.reply_text(settings.telegram_ack_message)
    await process_saved_submission(update, context, submission_id, text, "texte écrit")


async def audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not await is_authorized(update, settings):
        return
    message = update.effective_message
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

    max_bytes = settings.telegram_max_audio_mb * 1024 * 1024
    if media.file_size and media.file_size > max_bytes:
        await message.reply_text(f"Audio trop volumineux : limite configurée à {settings.telegram_max_audio_mb} Mo.")
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
        submission_id = await service.db.add_submission(
            chat_id=update.effective_chat.id,
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
        )
        if submission_id is None:
            final_path.unlink(missing_ok=True)
            return
        await message.reply_text(settings.telegram_ack_message)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        transcription = await service.groq.transcribe(final_path, mime_type)
        transcript = str(transcription["text"]).strip()
        await service.db.update_submission(
            submission_id,
            transcript=transcript,
            transcription_json=json.dumps(transcription, ensure_ascii=False),
            status="transcribed",
        )
        await message.reply_text(f"Transcription archivée :\n{transcript}")
        await process_saved_submission(update, context, submission_id, transcript, "audio transcrit")
    except Exception as exc:
        partial_path.unlink(missing_ok=True)
        LOG.exception("Réception/transcription audio échouée")
        submission = await service.db.get_submission(submission_id) if "submission_id" in locals() and submission_id else None
        if submission:
            await service.db.update_submission(submission_id, status="failed", failure_reason=str(exc)[:1000])
            await message.reply_text(f"Audio sauvegardé, mais traitement échoué (ID {submission_id}). Utilise /retry {submission_id}.")
        else:
            await message.reply_text("Impossible d'archiver cet audio. Consulte les logs du conteneur.")


async def morning_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    service = get_service(context)
    try:
        topic, _ = await service.make_topic()
        await send_text(context, settings.telegram_chat_id, service.format_topic(topic))
    except Exception as exc:
        LOG.exception("Tâche matinale échouée")
        await send_text(context, settings.telegram_chat_id, f"La génération du sujet a échoué : {str(exc)[:300]}")


async def evening_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    service = get_service(context)
    try:
        created = await service.generate_cards(settings.local_date())
        pushed, remaining = await service.push_pending_cards()
        await send_text(
            context,
            settings.telegram_chat_id,
            f"Bilan du soir\nFiches créées : {created}\nEnvoyées vers Anki : {pushed}\nEn attente : {remaining}",
        )
    except Exception as exc:
        LOG.exception("Tâche du soir échouée")
        await send_text(context, settings.telegram_chat_id, f"La création des fiches a échoué : {str(exc)[:300]}")


async def anki_retry_job(context: CallbackContext) -> None:
    settings: Settings = context.application.bot_data["settings"]
    try:
        pushed, remaining = await get_service(context).push_pending_cards()
        if pushed:
            await send_text(context, settings.telegram_chat_id, f"Nouvel essai Anki : {pushed} fiche(s) envoyée(s), {remaining} restante(s).")
    except Exception:
        LOG.exception("Nouvel essai Anki échoué")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await is_authorized(update, settings):
        await update.effective_message.reply_text("Format non pris en charge. Envoie du texte, une note vocale ou un fichier audio.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.exception("Erreur Telegram non gérée", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("topic", "Générer le sujet du jour"),
            BotCommand("cards", "Créer et pousser les fiches Anki"),
            BotCommand("stats", "Afficher les statistiques locales"),
            BotCommand("retry", "Retraiter une production par ID"),
            BotCommand("help", "Afficher l'aide"),
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

