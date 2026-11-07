FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
# Fail if the self-contained SQLCipher wheel is unavailable for this platform.
RUN python -m pip install --only-binary=:all: -r requirements.txt \
    && groupadd --gid 10001 aromat \
    && useradd --uid 10001 --gid aromat --no-create-home --shell /usr/sbin/nologin aromat \
    && mkdir /data \
    && chown aromat:aromat /data

COPY --chown=aromat:aromat . /app/

USER 10001:10001

CMD ["python", "-m", "bot"]
