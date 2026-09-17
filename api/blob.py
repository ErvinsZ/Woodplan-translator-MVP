"""Minimal Vercel Blob REST client -- there is no official Python SDK.

Reverse-engineered from @vercel/blob's own compiled source
(node_modules/@vercel/blob/dist/{index,chunk-YYMLUMXS}.js) rather than
guessed, since Vercel doesn't document the raw REST contract directly:
  - Upload: PUT https://vercel.com/api/blob/?pathname=<pathname>
  - Delete: POST https://vercel.com/api/blob/delete  {"urls": [...]}
  - Download: plain GET on the blob's own storage URL (the `url` field
    from an upload response), with the same bearer token -- this does
    NOT go through the vercel.com control-plane API above.
  - Headers: x-api-version (BLOB_API_VERSION, currently 12),
    x-vercel-blob-store-id (store id with any "store_" prefix
    stripped -- also embedded as the 4th "_"-separated segment of a
    read-write token), x-vercel-blob-access, authorization: Bearer.

Used instead of the Node client because api/translate.py needs to move
PDFs (often well over Vercel's 4.5MB function request/response body
cap) between the browser and PyMuPDF without ever holding the whole
file in a single Function's request or response body.
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

_API_URL = "https://vercel.com/api/blob"
_API_VERSION = "12"

# Some Python-runtime environments (seen in local `vercel dev`) don't have
# a system CA bundle linked, so the ambient SSL context fails every HTTPS
# request with CERTIFICATE_VERIFY_FAILED. Building the context from
# certifi's bundled CAs explicitly works regardless of the host's own
# certificate store.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _token():
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set")
    return token


def _store_id(token):
    # Matches @vercel/blob's parseStoreIdFromReadWriteToken: the 4th
    # "_"-separated segment of "vercel_blob_rw_<storeId>_<secret>".
    parts = token.split("_")
    return parts[3] if len(parts) > 3 else ""


def _headers(token, extra=None):
    headers = {
        "authorization": f"Bearer {token}",
        "x-api-version": _API_VERSION,
        "x-vercel-blob-store-id": _store_id(token),
    }
    headers.update(extra or {})
    return headers


def upload_blob(pathname, data, content_type, access="public"):
    """PUTs bytes to Blob storage, returns the parsed JSON response
    (url, downloadUrl, pathname, contentType, contentDisposition, etag).

    access must match the store's own configured access mode -- Blob
    enforces this per-store, not per-object, and rejects a mismatched
    upload with a plain 400 (e.g. "Cannot use public access on a
    private store").
    """
    token = _token()
    url = f"{_API_URL}/?pathname={urllib.parse.quote(pathname)}"
    headers = _headers(token, {
        "x-content-type": content_type,
        "x-add-random-suffix": "1",
        "x-vercel-blob-access": access,
    })
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, context=_SSL_CONTEXT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Blob upload failed ({e.code}): {e.read().decode()}") from e


def download_blob(blob_url):
    """GETs a blob's contents directly from its storage URL (not the
    control-plane API -- see module docstring)."""
    token = _token()
    request = urllib.request.Request(blob_url, headers=_headers(token))
    with urllib.request.urlopen(request, context=_SSL_CONTEXT) as response:
        return response.read()


def delete_blob(blob_url):
    token = _token()
    body = json.dumps({"urls": [blob_url]}).encode("utf-8")
    headers = _headers(token, {"content-type": "application/json"})
    request = urllib.request.Request(
        f"{_API_URL}/delete", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, context=_SSL_CONTEXT):
        pass
