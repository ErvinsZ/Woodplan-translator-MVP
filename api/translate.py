"""End-to-end translation endpoint: PDF in, translated PDF out.

Orchestrates extract -> classify_translate -> reinsert/export in one
serverless invocation, reusing the pure functions from the three pipeline
stage modules directly (no internal HTTP hop between them). Those modules
remain independently callable/testable for pipeline development; this is
the single entry point the frontend's upload-and-download flow calls.

PDFs move through Vercel Blob rather than the request/response body:
Vercel Functions cap both at 4.5MB, and real woodworking-plan PDFs (the
project's own sample is 15MB, translated output ~16.6MB) are routinely
well over that. The browser uploads the source PDF straight to Blob
(bypassing this function entirely) and posts the resulting blob URL
here; this function downloads from Blob, translates, uploads the result
as a new blob, and returns its download URL. See blob.py for the
hand-rolled REST client (no official Python SDK exists).

The store is public-access (Blob enforces access mode per-store, not
per-object -- a private store rejects any public upload outright), so
every blob's URL is directly downloadable by the browser with no token.
Protection today is an unguessable random-suffix pathname only; there's
no auth/ownership system yet to scope access more tightly, which is a
known simplification to revisit once feature/auth and feature/db-schema
exist.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Vercel's Python runtime loads the entrypoint via importlib (exec_module),
# not as a directly-run script, so this file's own directory is never
# auto-added to sys.path the way `python translate.py` would -- sibling
# imports silently 404 with ModuleNotFoundError without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract import extract_document
from classify_translate import classify_translate_document
from reinsert import reinsert_and_export
from blob import upload_blob, download_blob, delete_blob

# Only language with glossary data today (CLAUDE.md: build German
# end-to-end first, generalize later).
DEFAULT_TARGET_LANG = "de"


class handler(BaseHTTPRequestHandler):
    """POST JSON {"blob_url": "...", "filename": "plan.pdf"}, where
    blob_url is a blob the browser already uploaded directly to Vercel
    Blob. Optional query param ?lang=de picks the target language.
    Returns JSON {"download_url": "...", "filename": "..."} pointing at
    a new blob holding the translated PDF.
    """

    def do_POST(self):
        query = parse_qs(urlparse(self.path).query)
        target_lang = query.get("lang", [DEFAULT_TARGET_LANG])[0]

        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        source_blob_url = payload["blob_url"]
        source_name = payload.get("filename", "document.pdf")

        try:
            pdf_bytes = download_blob(source_blob_url)
        except Exception:
            self._send_json_error(400, "Could not fetch the uploaded file.")
            return

        try:
            document = extract_document(pdf_bytes)
        except Exception:
            self._send_json_error(400, "Could not read this file as a PDF.")
            return

        classify_translate_document(document, target_lang)
        output_bytes, _ = reinsert_and_export(pdf_bytes, document)

        translated_name = f"translated-{source_name}"
        result = upload_blob(
            translated_name, output_bytes, "application/pdf", access="public"
        )

        try:
            delete_blob(source_blob_url)
        except Exception:
            pass  # best-effort cleanup; a leftover private upload isn't fatal

        response = json.dumps({
            "download_url": result["downloadUrl"],
            "filename": translated_name,
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _send_json_error(self, status, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
