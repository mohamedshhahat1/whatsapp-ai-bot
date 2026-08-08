# ---- Stage 1: build the admin dashboard -------------------------------
# Node is only needed to produce static files; it never reaches the runtime
# image, so the production container stays a plain Python image.
#
# Pinned by digest, not by tag: `node:22-slim` is mutable and silently moves
# to a new build whenever upstream republishes it, so two builds of the same
# commit can differ. This is the multi-arch index digest, which keeps the
# image buildable on both the amd64 CI runner and an arm64 dev machine;
# pinning a single platform manifest would have broken one of them.
# Resolves to node 22 on debian bookworm-slim (22-bookworm-slim).
# Refresh with: docker buildx imagetools inspect node:22-slim
FROM node:22-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS dashboard

WORKDIR /dashboard

# Copy manifests first so the install layer is cached until dependencies
# change. Both are required and the lockfile is no longer globbed: if it ever
# goes missing the build should stop here, at COPY, with a clear message
# rather than carry on and install something else.
COPY dashboard/package.json dashboard/package-lock.json ./

# npm ci, unconditionally. It installs exactly what the lockfile pins, and it
# fails rather than resolving newer versions when the lockfile and
# package.json have drifted apart -- which is the entire reason to run it in a
# build. `npm install` is the wrong command here: it is free to move versions
# inside their caret ranges, so the image would not be reproducible.
RUN npm ci

COPY dashboard/ ./
RUN npm run build

# ---- Stage 2: application runtime -------------------------------------
# Digest-pinned for the same reason as the dashboard stage. This is also the
# layer Trivy actually scans, because the node stage is discarded, so keeping
# it reproducible is what makes a scan result mean anything.
# Resolves to python 3.12.13 on debian trixie-slim (3.12.13-slim-trixie).
# Refresh with: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

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
