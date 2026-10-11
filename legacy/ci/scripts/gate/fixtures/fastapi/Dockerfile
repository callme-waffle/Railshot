FROM python:3.12.14-slim-bookworm
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.18
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY app.py .
USER 10001
CMD ["/app/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
