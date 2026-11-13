FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "slack_rag.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
