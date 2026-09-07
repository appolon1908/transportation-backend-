FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

RUN addgroup --system freight && adduser --system --ingroup freight freight

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY workers ./workers
COPY migrations ./migrations
COPY compliance_migrations ./compliance_migrations
COPY alembic.ini alembic-compliance.ini ./
COPY deploy/backend/entrypoint-v4.sh /usr/local/bin/freight-entrypoint
RUN chmod 0555 /usr/local/bin/freight-entrypoint

USER freight
EXPOSE 8080

# Use the same composed API and explicit worker/migration modes as the governed
# release image. API startup never performs an implicit schema migration.
ENTRYPOINT ["freight-entrypoint"]
CMD ["api"]
