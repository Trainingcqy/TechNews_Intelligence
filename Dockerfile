FROM node:20-alpine AS trace-frontend-build

WORKDIR /frontend

COPY trace_dashboard/package.json trace_dashboard/package-lock.json ./
RUN npm ci

COPY trace_dashboard/ ./
RUN npm run build

FROM python:3.12-slim AS python-runtime

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY agent/ ./agent/
COPY app/ ./app/
COPY services/ ./services/
COPY ingest/ ./ingest/

CMD ["python", "-m", "app.bot"]

FROM python-runtime AS trace-runtime

COPY --from=trace-frontend-build /frontend/dist ./trace_dashboard/dist

CMD ["uvicorn", "app.trace_api:app", "--host", "0.0.0.0", "--port", "8010"]
