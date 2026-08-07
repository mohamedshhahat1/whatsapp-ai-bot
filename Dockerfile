# ---- Stage 1: build the admin dashboard -------------------------------
# Node is only needed to produce static files; it never reaches the runtime
# image, so the production container stays a plain Python image.
FROM node:22-slim AS dashboard

WORKDIR /dashboard

# Copy manifests first so the install layer is cached until dependencies
# change. The glob makes the lockfile optional, because there is not one yet.
COPY dashboard/package.json dashboard/package-lock.json* ./

# npm ci is the reproducible install: it installs exactly what the lockfile
# pins and fails if the lockfile and package.json have drifted apart. It also
# fails hard when no lockfile exists at all, and this repository has no
# dashboard/package-lock.json -- so an unconditional `npm ci` here would break
# every image build rather than make it reproducible.
#
# Run `npm install` once in dashboard/ and commit the lockfile; this step then
# tightens automatically with no change needed here. Same shape as the
# frontend job in .github/workflows/ci.yml, deliberately.
RUN if [ -f package-lock.json ]; then \
        echo "lockfile found - using npm ci"; \
        npm ci; \
    else \
        echo "WARNING: dashboard/package-lock.json is missing; falling back to npm install"; \
        npm install; \
    fi

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
