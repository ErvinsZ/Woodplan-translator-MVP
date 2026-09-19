"""Minimal Postgres connection helper for the dynamic translation cache.

Runtime queries from api/*.py go through the POOLED `DATABASE_URL`, never
`DIRECT_URL` (that one is CLI/migration-only -- see CLAUDE.md's Prisma 7
notes: running DDL through the pooled connection hangs, but pooled is the
correct choice for many concurrent serverless reads/writes). There is no
Prisma Python client, so this talks to Postgres directly via psycopg
rather than through Prisma -- Prisma only owns the schema/migrations
(prisma/schema.prisma), not runtime access, on this side of the stack.
"""

import os

import psycopg


def get_connection():
    """Opens a new short-lived connection.

    Not pooled at the process level -- Vercel Python function instances
    aren't guaranteed to be reused between invocations, so a persistent
    client-side pool would mostly just leak connections rather than help.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(database_url)
