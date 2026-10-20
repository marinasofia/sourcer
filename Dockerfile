# Pipeline image: runs one scheduled pass and exits.
# Build: docker build -t sourcer .
# Run:   docker run --rm -e ANTHROPIC_API_KEY=... -v "$PWD/data:/data" sourcer
FROM python:3.15-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the layer. Hashes are
# verified against the uv-compiled lockfile.
COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY sourcer/ sourcer/
RUN pip install --no-deps .

# the operator's skills vector and export go into this image: build it locally
# or into a private registry, never a public one
COPY companies.json skills_vector.json ./
COPY docs/data.json docs/data.json

# Run as an unprivileged user; SQLite lives on a mounted volume so results
# persist across runs.
RUN useradd --system --uid 10001 --create-home sourcer \
    && mkdir -p /data && chown sourcer:sourcer /data
USER 10001:10001
ENV SOURCER_ROOT=/app \
    SOURCER_DB=/data/sourcer.db \
    SOURCER_COMPANIES=/app/companies.json \
    SOURCER_SKILLS=/app/skills_vector.json
VOLUME ["/data"]

CMD ["python", "-m", "sourcer.main"]
