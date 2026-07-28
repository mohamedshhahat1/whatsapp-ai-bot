# ---- Stage 1: build the admin dashboard -------------------------------
# Node is only needed to produce static files; it never reaches the runtime
# image, so the production container stays a plain Python image.
FROM node:22-slim AS dashboard

WORKDIR /dashboard

# Copy manifests first so `npm ci` is cached until dependencies change.
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm install

COPY dashboard/ ./
RUN npm run build

# ---- Stage 2: application runtime -------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Built SPA, served by FastAPI at /dashboard.
COPY --from=dashboard /dashboard/dist ./dashboard/dist

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
