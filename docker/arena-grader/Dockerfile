# Sandbox for running model-written solutions against problem tests.
# Build: docker build -t arena-grader docker/arena-grader
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Same pytest version as uv.lock. Tests run as an unprivileged user.
RUN pip install --no-cache-dir pytest==9.1.1 \
    && useradd --create-home --uid 10001 grader

USER grader
WORKDIR /work
