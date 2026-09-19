"""Minimal Postgres connection helper for the dynamic translation cache.

Runtime queries from api/*.py go through the POOLED `DATABASE_URL`, never
`DIRECT_URL` (that one is CLI/migration-only -- see CLAUDE.md's Prisma 7
notes: running DDL through the pooled connection hangs, but pooled is the
correct choice for many concurrent serverless reads/writes). There is no
Prisma Python client, so this talks to Postgres directly rather than
through Prisma -- Prisma only owns the schema/migrations
(prisma/schema.prisma), not runtime access, on this side of the stack.

Uses pg8000 (pure Python, no compiled extension), NOT psycopg. psycopg's
binary wheel (compiled libpq/psycopg_c) failed to import in Vercel's
Python function runtime with "no pq wrapper available" / "libpq library
not found" -- the same class of failure google-genai's compiled
pydantic-core dependency hit (see gemini_translate.py's docstring).
Vercel's Python build/runtime split doesn't reliably support compiled C
extensions; prefer pure-Python packages here unless there's truly no
alternative.

pg8000 has no built-in connection-string parsing, so DATABASE_URL is
parsed by hand. It also needs an explicit SSL context built from
certifi's CA bundle -- same reason as blob.py's urllib client: the
ambient SSL context in some Python-runtime environments doesn't have a
usable CA bundle linked, which fails Neon's required TLS handshake with
CERTIFICATE_VERIFY_FAILED.
"""

import os
import ssl
import urllib.parse

import certifi
import pg8000.dbapi


def get_connection():
    """Opens a new short-lived connection.

    Not pooled at the process level -- Vercel Python function instances
    aren't guaranteed to be reused between invocations, so a persistent
    client-side pool would mostly just leak connections rather than help.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    parsed = urllib.parse.urlparse(database_url)
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    return pg8000.dbapi.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=urllib.parse.unquote(parsed.username),
        password=urllib.parse.unquote(parsed.password),
        ssl_context=ssl_context,
    )
