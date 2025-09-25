import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional

import psycopg2
from psycopg2 import Error, OperationalError
from psycopg2.extras import DictCursor

from pdfminer.high_level import extract_text

logger = logging.getLogger(__name__)

TABLE_NAME = "pdf_documents"


class DatabaseUnavailable(RuntimeError):
    """Raised when PostgreSQL cannot be reached."""


@lru_cache(maxsize=1)
def _connection_parameters() -> dict:
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "pdfscraper"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", "postgres"),
    }


def _get_connection():
    params = _connection_parameters()
    try:
        connection = psycopg2.connect(**params)
    except OperationalError as exc:  # pragma: no cover - requires DB outage
        raise DatabaseUnavailable("Unable to connect to PostgreSQL") from exc
    return connection


def create_index() -> None:
    """Ensure the PostgreSQL tables and indices required for search exist."""

    ddl_statements = [
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            sha256 TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            size BIGINT NOT NULL,
            url TEXT NOT NULL,
            source_page TEXT,
            downloaded_at TIMESTAMPTZ NOT NULL,
            content TEXT NOT NULL,
            search_vector tsvector
                GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(content, '')), 'B')
                ) STORED
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_search
        ON {TABLE_NAME}
        USING GIN (search_vector)
        """,
    ]

    connection = _get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                for statement in ddl_statements:
                    cursor.execute(statement)
    except DatabaseUnavailable:
        raise
    except Error as exc:
        raise RuntimeError("Failed to initialize PostgreSQL index") from exc
    finally:
        connection.close()

    logger.info("Ensured PostgreSQL table '%s' exists", TABLE_NAME)


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract textual content from a PDF file."""

    try:
        return extract_text(pdf_path)
    except Exception as exc:  # pragma: no cover - pdfminer specific errors
        logger.warning("Failed to extract text from %s: %s", pdf_path, exc)
        return ""


def _file_sha256(pdf_path: Path) -> str:
    import hashlib

    hasher = hashlib.sha256()
    with open(pdf_path, "rb") as file_pointer:
        for chunk in iter(lambda: file_pointer.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_downloaded_at(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Invalid downloaded_at value '%s', defaulting to now", value)
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def index_pdf(
    pdf_path: str,
    pdf_url: str,
    *,
    source_page: Optional[str] = None,
    downloaded_at: Optional[str] = None,
) -> Optional[str]:
    """Index a PDF file in PostgreSQL and return its unique hash."""

    path = Path(pdf_path)
    if not path.exists():
        logger.warning("Cannot index missing file %s", pdf_path)
        return None

    pdf_content = extract_pdf_text(path)
    if not pdf_content.strip():
        logger.info("No text extracted from %s; skipping indexing", path.name)
        return None

    document_hash = _file_sha256(path)
    downloaded_ts = _parse_downloaded_at(downloaded_at)

    connection = _get_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT 1 FROM {TABLE_NAME} WHERE sha256 = %s",
                    (document_hash,),
                )
                if cursor.fetchone():
                    logger.info("Document %s already indexed", path.name)
                    return document_hash

                cursor.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (
                        sha256, name, size, url, source_page, downloaded_at, content
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document_hash,
                        path.name,
                        path.stat().st_size,
                        pdf_url,
                        source_page,
                        downloaded_ts,
                        pdf_content,
                    ),
                )
    except DatabaseUnavailable:
        raise
    except Error as exc:
        raise RuntimeError("Failed to store PDF metadata") from exc
    finally:
        connection.close()

    logger.info("Indexed %s", path.name)
    return document_hash


def index_multiple(
    documents: Iterable[dict],
) -> int:
    """Index multiple PDFs using PostgreSQL."""

    indexed = 0
    for doc in documents:
        try:
            if index_pdf(
                doc["path"],
                doc["url"],
                source_page=doc.get("source_page"),
                downloaded_at=doc.get("downloaded_at"),
            ):
                indexed += 1
        except DatabaseUnavailable:
            raise
        except RuntimeError:
            raise
    return indexed


def search_pdfs(
    query: str,
    *,
    size: int = 20,
) -> List[dict]:
    """Search indexed PDFs using PostgreSQL full-text search."""

    if not query:
        return []

    connection = _get_connection()
    try:
        with connection.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    sha256,
                    name,
                    url,
                    source_page,
                    downloaded_at,
                    content,
                    ts_headline('english', content, websearch_to_tsquery('english', %s),
                        'MaxFragments=1, MinWords=10, MaxWords=50') AS snippet
                FROM {TABLE_NAME}
                WHERE search_vector @@ websearch_to_tsquery('english', %s)
                ORDER BY downloaded_at DESC
                LIMIT %s
                """,
                (query, query, size),
            )
            rows = cursor.fetchall()
    except OperationalError as exc:  # pragma: no cover - requires DB outage
        raise DatabaseUnavailable("Unable to execute search query") from exc
    except Error as exc:
        raise RuntimeError("Failed to execute search query") from exc
    finally:
        connection.close()

    results = []
    for row in rows:
        downloaded_at = row["downloaded_at"]
        if isinstance(downloaded_at, datetime):
            downloaded_iso = downloaded_at.astimezone(timezone.utc).isoformat()
        else:
            downloaded_iso = str(downloaded_at)

        highlight = [row["snippet"]] if row["snippet"] else []
        results.append(
            {
                "_id": row["sha256"],
                "_source": {
                    "name": row["name"],
                    "url": row["url"],
                    "source_page": row["source_page"],
                    "downloaded_at": downloaded_iso,
                    "content": row["content"],
                },
                "highlight": {"content": highlight} if highlight else {},
            }
        )

    return results
