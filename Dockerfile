FROM node:24-alpine AS trace-frontend-build

WORKDIR /frontend

COPY web/trace/package.json web/trace/package-lock.json ./
RUN npm ci

COPY web/trace/ ./
RUN npm run build

FROM python:3.12-slim AS python-runtime

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY agent/ ./agent/
COPY app/ ./app/
COPY common/ ./common/
COPY config/ ./config/
COPY ingest/ ./ingest/

CMD ["python", "-m", "app.bot"]

FROM python-runtime AS trace-runtime

COPY --from=trace-frontend-build /frontend/dist ./web/trace/dist

CMD ["uvicorn", "app.trace_api:app", "--host", "0.0.0.0", "--port", "8010"]
