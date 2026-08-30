FROM ghcr.io/astral-sh/uv:0.11.23 AS uv

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=app:app bot.py ./bot.py
COPY --chown=app:app streamlit_app.py ./streamlit_app.py
COPY --chown=app:app dashboard_pages ./dashboard_pages
COPY --chown=app:app .streamlit ./.streamlit
COPY --chown=app:app english_teacher ./english_teacher

USER app

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "english_teacher", "--healthcheck"]

CMD ["python", "-u", "-m", "english_teacher"]
