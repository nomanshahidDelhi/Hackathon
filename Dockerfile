# SRE agent backend + console in one Cloud Run service.
# Stage 1 builds the React console; stage 2 runs FastAPI (REST + AG-UI) with the
# built console served as static files. bash is present for the remediation
# sandbox (PATH inside the sandbox holds only stubs; no credentials are passed).

FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY agent/requirements.txt agent/requirements.txt
COPY ingest/requirements.txt ingest/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt -r ingest/requirements.txt
COPY agent/ agent/
COPY executor/ executor/
COPY ingest/ ingest/
COPY --from=console /console/dist console/dist
RUN useradd --create-home app && chown -R app /app
USER app
# The same image runs the ingest consumer: override the command with
#   python -m ingest.consumer
CMD ["sh", "-c", "exec uvicorn agent.app.server:app --host 0.0.0.0 --port ${PORT}"]
