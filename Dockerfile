# One image, four entrypoints.
#
# The agents, the pipeline, the API and the console run from the *same* image and
# differ only in their command. That is deliberate: the pipeline stages are the
# same code the test suite exercises, the API imports the same agent registry, and
# there is exactly one artifact to build, scan, sign and promote. Four images would
# mean four chances for the deployed pipeline to differ from the tested one.
#
# Two stages, because a build toolchain has no business in a runtime image. The
# result is a distroless-adjacent image with no compiler, no package manager cache
# and no root user.
#
#   build:   uv resolves the locked dependency set and installs the project
#   runtime: a non-root user, the resolved virtualenv, and the data contracts
#
# Build it with the git SHA as both the tag and an argument, so the image can say
# which commit it is running:
#
#   docker build --build-arg BDA_VERSION=$(git rev-parse --short HEAD) -t bda:$(git rev-parse HEAD) .
#
# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: resolve and install
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

# uv from its official image rather than pip-installing it: one fewer network
# round trip, and the same uv the repository is developed with.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # The base image's interpreter is the one to use; never download another.
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, project second, so a source-only change does not invalidate
# the dependency layer. This is the difference between a 20-second rebuild and a
# four-minute one.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The application, the contracts and the semantic layer. Contracts ship with the
# image on purpose: an agent that cannot read its own contracts cannot refuse the
# queries those contracts prohibit.
COPY src ./src
COPY contracts ./contracts
COPY semantic ./semantic
COPY README.md ./

# Build metadata, so `bda --version` and the evidence envelope can name the commit.
ARG BDA_VERSION=0.0.0-dev
ENV BDA_VERSION=${BDA_VERSION}

RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="banking-data-agents" \
      org.opencontainers.image.description="Governed data product agents on Amazon Bedrock AgentCore" \
      org.opencontainers.image.licenses="MIT"

# A fixed uid, not "the first free one": a volume or a policy that names the uid
# should not depend on build order.
RUN groupadd --system --gid 1001 bda \
    && useradd --system --uid 1001 --gid bda --home-dir /app --shell /usr/sbin/nologin bda

WORKDIR /app

COPY --from=build --chown=bda:bda /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    # Never read a stray .env from the image: configuration arrives as environment
    # variables or from Secrets Manager, so that what is deployed is what was
    # reviewed.
    BDA_ENV_FILE=/dev/null

USER bda

# The API and console listen on 8000; the agent runtimes override the port through
# AgentCore and the pipeline ignores it entirely.
EXPOSE 8000

# Runs as the non-root user, and exercises the same route the App Runner and
# AgentCore health checks use.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

# Overridden by the pipeline (`bda pipeline run`) and the console
# (`streamlit run ...`); the default is the analyst-facing surface.
CMD ["bda", "serve", "--host", "0.0.0.0", "--port", "8000"]
