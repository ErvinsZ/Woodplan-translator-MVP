# Project State

Last updated: 2026-09-22. `feature/layout-rebuild` merged to `dev` (PR
#12), then `dev` merged to `main` (PR #13) and deployed to Production —
confirmed `READY` for commit `b13945d` via the Vercel API, and a live
GET against the deployed site (`/` 200, `/api/extract` 501 as expected).
See §6 for the full rebuild writeup; the "OCR broken on localhost"
report was a missing Vercel env var, now fixed (§7). Earlier (2026-09-20):
after the user tested the OCR fallback (`feature/ocr-fallback`, branched
off `dev`) against a real 55-page seller PDF
(`reference/sample_with_raft_img.pdf`) and found a real gap (pages with
real vector text AND a separately-baked-in-text embedded image, e.g. a
full materials table as one image, were silently completely untouched),
added per-image OCR (`ocr_image_regions`), which then surfaced its own
real bug on the same real PDF (rendering an image's page-region for OCR
also rendered real vector text positioned near it, causing doubled/
ghosted text), fixed by OCR-ing each embedded image's raw pixel data
directly instead. Both fixes were confirmed working via DIRECT PYTHON
PIPELINE CALLS in a venv (not through the actual running web app) —
committed (`c4ae952`) on that basis, but the user then reported OCR
still doesn't work when actually tested through localhost, which the
commit-time verification apparently did not catch. Before that:
verifying the OCR fallback against the real Vision API for the first
time (two more real bugs found and fixed: an 8x-too-tight rotation
epsilon, under-tuned erasure padding). Before that: the font-size-consistency fix (PR
#10, merged to `dev` and `main`); Google Drive autosave (PR #8, merged
to `dev`) confirmed working end-to-end by the user (sign in, toggle on,
translate, file landed in Drive), while the OAuth consent screen is
still in Google's Testing status and the user is working through
Google's publishing/verification flow to open it up beyond the
developer's own account.
Before that: `dev` merged to `main`, deployed to a real Production
environment, Vercel's SSO deployment protection (which was blocking
Production entirely) resolved, Google sign-in confirmed working
end-to-end on Production/`dev` Preview/`localhost:3000`.
Before that: `feature/google-auth` merged to `dev` (PR #6); before
that, a Vercel build failure was found + fixed post-merge (missing
Prisma client generation) and a broken `NEXTAUTH_URL` value on the `dev`
Preview deployment was found + fixed; before that, adding user database
+ Google authentication on `feature/google-auth` and provisioning its
env vars in Vercel.
Before that: a second pass fixing `reinsert.py` rendering bugs
(redaction border artifact over diagrams; hardcoded font; overflow
bleed/wrap-into-diagram, resolved via a deep-shrink-first fallback) plus
one `glossary.py` content rebalance, verified against real broken-output
repros the user generated locally — not yet on a merged branch as of that
entry, see §6. Before that: merged dynamic Gemini translation +
Spanish/French support (PR #4).
See `CLAUDE.md` for the original plan/spec; this file tracks actual current state,
which has diverged from and extended that plan in places noted below.

## 1. Project goal

A SaaS tool that takes an Etsy seller's woodworking/DIY build-plan PDFs (English)
and produces localized versions (German, Spanish, French) while preserving exact technical
layout — dimension callouts, part codes, cutout lists, scale diagrams stay
byte-identical; only translatable prose text moves. Targets small Etsy plan
sellers, underserved between expensive CAD-translation agencies and generic PDF
translators that don't understand dimensioned technical drawings.

## 2. Current architecture

```
[Browser] --upload PDF--> [Vercel Blob] <--download/upload--> [api/translate.py]
                                                                      |
                                              extract -> classify_translate -> reinsert
```

**Pipeline (Python, Vercel serverless functions, one file = one function):**
- `api/extract.py` — PyMuPDF text-span extraction from a PDF page. Exports
  `extract_page(page, page_index)`, `extract_document(pdf_bytes_or_path, name)`,
  plus an HTTP `handler` class (POST raw PDF or `{pdf_base64, page_index}` JSON,
  returns extracted JSON). Defines the core JSON schema every later stage reads/
  writes (see `pipeline` sub-object below). Every text_box now also carries
  `"source": "vector"` or `"source": "ocr"` (see below). `extract_document`
  calls into `ocr_vision.py` for two distinct cases: the WHOLE page for a
  page with zero real vector text boxes (a flattened/rasterized scan or
  export-to-image plan, per CLAUDE.md's original architecture note), or
  EACH embedded image individually for a page that does have real vector
  text but also contains one or more raster images with their own
  separately baked-in text (found on a real seller PDF -- a full
  materials table baked entirely into one embedded image on an otherwise
  normal page). Either way, an OCR failure (no API key, Vision error) is
  caught per-page/per-image and just leaves that content untouched, same
  as if the fallback didn't exist -- it only ever adds coverage.
- `api/geometry.py` — `rotation_angle(line_dir)`, split out of extract.py
  into its own dependency-free module specifically so `ocr_vision.py` can
  use the exact same angle math without a circular import (`extract.py`
  needs to import `ocr_vision.py` for the fallback above; `ocr_vision.py`
  needed the same rotation calculation extract.py already had).
- `api/ocr_vision.py` — Google Cloud Vision `DOCUMENT_TEXT_DETECTION`,
  called by `extract_document` for TWO distinct cases: `ocr_page` for a
  fully flattened page (renders the whole page via `page.get_pixmap`),
  and `ocr_image_regions` for individual embedded raster images with
  their own baked-in text on an otherwise-normal (has-real-vector-text)
  page -- see below and its own module docstring for why these two use
  genuinely different rendering strategies, not just different scope.
  POSTs to Vision's REST API via a plain API key (`GOOGLE_VISION_API_KEY`)
  + `urllib` -- deliberately NOT a service-account/OAuth2 flow, which
  would need a crypto library to sign JWTs (PyJWT/cryptography or
  google-auth's own dependency chain) -- `cryptography` ships a compiled
  Rust extension, exactly the class of dependency that already crashed
  real Vercel Python deployments once this project (`google-genai`,
  `psycopg[binary]` -- see §4). Reconstructs one text_box per VISUAL LINE
  from Vision's word/symbol hierarchy (Vision has no explicit line
  grouping, only a break-type hint --
  SPACE/EOL_SURE_SPACE/LINE_BREAK/HYPHEN -- on each word's last symbol),
  converts coordinates back to PDF points, and estimates a font size from
  word-box height (Vision gives no font size, there's no real original
  font to match here anyway). Every emitted text_box carries
  `"source": "ocr"`.
  - `ocr_image_regions` OCRs each embedded image's OWN RAW PIXEL DATA,
    extracted directly via its PDF xref (`_ocr_raw_image`) -- NOT a
    `get_pixmap(clip=...)` render of that image's page-space region
    (which is exactly what `ocr_page` does, correctly, for a genuinely
    empty whole page). Root-caused the hard way on a real seller PDF:
    rendering a region for OCR also renders any real vector text
    positioned on top of or near that embedded image, so Vision
    re-detected text ALREADY correctly captured by normal vector
    extraction, and both got drawn independently at slightly different
    positions -- visible as literal doubled/ghosted text in the real
    output. OCR-ing the raw image bytes directly is immune to this by
    construction. Also skips images below a minimum size (not worth an
    API call) and images mostly overlapped by an already-processed
    image on the same page (also found for real: a "zoomed detail"
    callout image overlapping three of a page's other four images).
  - **A real bug was caught by the module's own mocked-response tests,
    not left in**: the rotation-angle calculation originally used
    `dx or 1` as a defensive fallback for a degenerate zero-vector case
    that doesn't actually exist (`atan2(0, 0)` returns `0.0` natively,
    no guard needed) -- since `0` is falsy in Python, `dx or 1` silently
    forced `dx` to `1` whenever the TRUE dx was legitimately `0`, which
    is exactly the correct value for genuinely vertical (90-degree)
    text. Caught immediately by a mocked-vertical-text test expecting
    exactly `-90.0` and getting `-89.4` instead; fixed by only
    substituting the `(1, 0)` fallback when both dx AND dy are zero
    (a truly degenerate same-point box), not whenever dx alone is zero.
- `api/layout_units.py` — groups text into translation UNITS (see its
  docstring): spans -> lines -> clusters -> paragraphs -> units, with
  table detection (table-like clusters become one unit per cell),
  exact-duplicate line merge, styled lead-in codes, OCR lines grouped by
  image + Vision paragraph. Also `extract_segments` (drawn straight
  lines). Called by extract.py (`extract_page_full`), which stores
  `page["units"]` and `page["segments"]`; reinsert.py reuses
  `page_lines` for obstacles.
- `api/masking.py` — numbers/dimensions/part codes -> ⟦N⟧ placeholders
  before any LLM call, restored byte-for-byte; translations with altered
  placeholders are rejected. `numbers_preserved` vets cache hits.
- `api/classify_translate.py` — translates each unit PARAGRAPH (not
  span) and fills `unit["pipeline"]` (`translated_paragraphs`,
  `classification`, `needs_review`). Tiers: code-only (unchanged) /
  whole-paragraph glossary / every-line glossary joined (keeps the
  span-built glossary useful) / per-token glossary (codes via masking
  regex) / whole-document cache + masked Gemini (deduped by masked text,
  so "STEP 1."…"STEP 8." are one item). Glossary hits keep an ALL-CAPS
  source's case. Translated OCR paragraphs with digits read at Vision
  confidence < 0.9 are flagged `needs_review`. Text boxes keep
  `pipeline.unit_id`.
- `api/glossary.py` — pure data, checked into git. `GLOSSARY[english_text][lang_code]
  = translation`. 114 German entries, built by sweeping all 15 pages of the
  sample PDF; ~0 Spanish/French entries so far (see §4 — the dynamic cache
  fills this gap from real usage instead of a manual curation pass).
- `api/translation_cache.py` — the DYNAMIC counterpart to `api/glossary.py`:
  a Postgres-backed cache (`glossary_terms` table) of Gemini-generated
  translations, keyed on stripped+lowercased source text (deliberately
  different from `GLOSSARY`'s raw-whitespace-preserving keys — see the
  module's own docstring for why). `lookup_many` / `save_many`, both fail
  soft on any DB error (cache is an optimization, not a correctness
  requirement). Uses `api/db.py` for the actual connection.
- `api/db.py` — Postgres connection helper via `pg8000` (pure Python, NOT
  psycopg — see §4 for why that matters). Parses `DATABASE_URL` by hand
  (pg8000 has no DSN parsing) and builds an explicit `certifi`-based SSL
  context, same reasoning as `blob.py`'s SSL setup below.
- `api/gemini_translate.py` — calls the Gemini API (`gemini-3.1-flash-lite`,
  Google AI Studio free tier) directly via `urllib` REST calls, NOT the
  `google-genai` SDK (see §4 for why). **Don't reintroduce
  `gemini-2.5-flash-lite`** — the model originally planned for its
  free-tier limits — it 404s ("no longer available to new users") on this
  project's API key despite still appearing in the `/models` listing;
  confirm any model choice against a real `GET /v1beta/models` call or the
  live AI Studio dashboard, not training data or search results, since
  Google changes free-tier model availability without much notice. System
  prompt includes the static
  glossary's existing target-language terms so Gemini stays consistent
  with established phrasing. Chunks + paces sequential calls in-process to
  respect the free-tier rate limit (15 req/min, 500 req/day, 250k
  tokens/min as of 2026-09-19 — re-check the live AI Studio dashboard,
  Google changes these), retries once or twice with backoff on 429/5xx,
  and never raises past a single chunk's failure — a failed chunk just
  leaves its spans `unmatched`/`needs_review=True`, same as if the
  glossary had never covered them.
- `api/reinsert.py` — per-unit re-layout (see its docstring): only
  changed units are touched (codes/numbers keep original glyphs);
  glyph-only redaction (`fill=False`, inset rect, images/line-art NONE,
  TEXT_REMOVE); layout box grows until a real obstacle (drawn/raster line
  segments, other text, placed boxes, bounds, 72-dpi `PixelProbe` ink);
  callout labels pinned on their right grow left; single-line table cells
  never grow down; OCR units anchored on their measured baseline;
  `insert_htmlbox` (scale_low 0.75, then 0 + needs_review). Rotation
  0/90/180/270 only (others left untranslated + flagged). OCR text in an
  embedded image is erased by inpainting (`api/image_erase.py`, numpy
  diffusion fill) and `replace_image` (JPEG stays JPEG, alpha kept), once
  per image per document; whole-page OCR words are covered with the
  sampled background colour.
- `api/image_erase.py` — numpy stand-in for `cv2.inpaint` (opencv is too
  big for Vercel, see CLAUDE.md).
- `api/translate.py` — the only endpoint the frontend calls. Orchestrates the
  three stages above in one invocation (no internal HTTP hop). Downloads
  source PDF from a Blob URL, runs the pipeline, uploads result to Blob,
  returns `{"download_url", "filename"}` JSON. **Every one of these `api/*.py`
  files that imports a sibling module has this exact line near the top** —
  required, do not remove:
  ```python
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  ```
  (Vercel loads entrypoints via `importlib.exec_module`, which does NOT
  auto-add the script's own directory to `sys.path` — see §7.)
- `api/blob.py` — hand-rolled Vercel Blob REST client (no official Python
  SDK exists). `upload_blob(pathname, data, content_type, access="public")`,
  `download_blob(url)`, `delete_blob(url)`. Reverse-engineered from
  `@vercel/blob`'s own compiled JS in `node_modules/@vercel/blob/dist/`, not
  guessed — see that module's docstring for the exact REST contract
  (endpoints, headers, auth). Uses `certifi`'s CA bundle explicitly (ambient
  SSL context in the Lambda's Python env is unreliable — see §7).

**Frontend (Next.js 16 App Router, TypeScript):**
- `app/page.tsx` — the entire UI. Client component (`"use client"`). A
  DE/ES/FR language toggle picks `lang`. Upload (drag-drop or click) ->
  `@vercel/blob/client`'s `upload()` sends the PDF straight to Blob
  storage -> POST `{blob_url, filename}` to `/api/translate?lang=<code>`
  -> renders the returned `download_url` as a link. No login, no
  persistence, no history — single-shot tool.
- `app/api/upload-token/route.ts` — Next.js Route Handler. Issues short-lived
  client-upload tokens via `@vercel/blob/client`'s `handleUpload()`. This is
  the ONLY Next.js API route in the project; everything else backend-side is
  a Python function under top-level `api/`. These are two independent routing
  systems that both map into `/api/*` — do not create a Next.js route at a
  path that collides with an `api/*.py` filename.
- `app/layout.tsx` — root layout, Space Grotesk + IBM Plex Mono fonts
  (`next/font/google`), metadata.
- `app/globals.css` — design tokens as CSS custom properties (`--background`,
  `--foreground`, `--paper`, `--line`, `--line-strong`, `--accent`,
  `--blueprint`, `--muted`), light + dark variants, exposed to Tailwind v4 via
  `@theme inline`. Dot-grid background texture on `body`.
- `app/providers.tsx` — client component wrapping `children` in next-auth's
  `SessionProvider`, mounted in `app/layout.tsx` around the whole `<body>`.
- `app/components/AuthStatus.tsx` — client component using `useSession`;
  renders a "Sign in with Google" button (top-right bar in `layout.tsx`) or
  the signed-in email + "Sign out". No route is gated behind auth yet — the
  upload/translate flow on `/` still works fully signed-out, same as
  before. This is intentional for now: this session only wired up
  identity/DB, not access control — see §6.
- `lib/prisma.ts` — Prisma Client singleton for the **Node/Next.js** side
  (distinct from `api/db.py`'s pg8000 connection, which is Python-only and
  unrelated). Uses `@prisma/adapter-pg` + pooled `DATABASE_URL`, per
  CLAUDE.md's Prisma 7 driver-adapter requirement. Cached on
  `globalThis` to survive Next dev's hot-reload without exhausting Neon's
  connection pool.
- `lib/auth.ts` — next-auth v4 `authOptions`: `PrismaAdapter` (from
  `@auth/prisma-adapter`, NOT the older/deprecated
  `@next-auth/prisma-adapter`), Google provider, **database** session
  strategy (not JWT) so sessions persist in Postgres across stateless
  function invocations. `session` callback copies `user.id` +
  `driveAutosaveEnabled` onto `session.user` (typed via
  `types/next-auth.d.ts`). Google provider requests
  `drive.file`/`access_type=offline`/`prompt=consent` scope UPFRONT at
  every sign-in, not as a separate incremental-consent flow -- a
  deliberate simplicity trade-off (every user sees the Drive permission
  request, and sees Google's consent screen on every sign-in, not just
  the first) in exchange for reusing NextAuth's existing account-linking
  instead of a second OAuth callback route. Refresh token is encrypted
  (`lib/crypto.ts`) via TWO mechanisms, both needed: (1) a `linkAccount`
  wrapper around the adapter, which only fires for a brand-new account;
  (2) an `events.signIn` handler, which fires on EVERY sign-in (new or
  repeat) with the raw OAuth `account` object regardless of what the
  adapter did with it. (2) exists because
  `node_modules/next-auth/core/lib/callback-handler.js` was read
  directly and confirmed: for an ALREADY-linked account, NextAuth v4
  skips `linkAccount` entirely and just creates a session -- any fresh
  refresh_token from that sign-in's re-consent would otherwise be
  silently discarded, meaning a user who signed in before this feature
  existed would never actually get a Drive token stored just by signing
  in again. Confirmed via source, not assumed.
- `app/api/auth/[...nextauth]/route.ts` — the NextAuth App Router handler
  (`NextAuth(authOptions)`, exported as both `GET`/`POST`).
- `lib/crypto.ts` — AES-256-GCM `encrypt`/`decrypt`, keyed by
  `ENCRYPTION_KEY` (32 random bytes, base64 in env, shared across all
  three Vercel environments like `NEXTAUTH_SECRET`). Node's built-in
  `crypto`, no new dependency. Used specifically for the Drive
  `refresh_token` at rest, per CLAUDE.md's requirement.
- `lib/drive.ts` — hand-rolled Drive v3 REST calls (no `googleapis`
  client, matching `api/blob.py`'s/`api/gemini_translate.py`'s existing
  pattern). `saveTranslationToDrive(userId, blobUrl, filename)`: decrypts
  the user's stored refresh_token, exchanges it for a fresh access_token
  (always refreshes on use rather than caching one -- simple and correct
  for a stateless serverless function), finds-or-creates the user's Drive
  folder, fetches the translated PDF from its Blob URL, uploads it.
  `findOrCreateFolder` verifies a stored `driveFolderId` still resolves
  (not trashed/deleted) before reusing it -- a Drive file ID survives the
  user renaming the folder, only a delete/trash invalidates it, so this
  directly answers "what if the user renames the folder."
- `app/api/drive/toggle/route.ts` (POST) — flips `User.driveAutosaveEnabled`
  for the signed-in user. No re-consent needed; the scope was already
  granted at sign-in (see `lib/auth.ts` below).
- `app/api/drive/save/route.ts` (POST) — takes `{blob_url, filename}`,
  requires a session with `driveAutosaveEnabled` true, calls
  `saveTranslationToDrive`. Called by the frontend right after a
  successful `/api/translate`, purely additive -- never blocks or alters
  the existing download-button flow.

**Reference/test fixtures (not app code):**
- `reference/sample_plan.pdf` — real 15-page Etsy plan, the only test input
  used throughout. `reference/pipeline_tested.py` — original single-file
  proof-of-concept the whole pipeline was refactored out of (has known bugs,
  see CLAUDE.md §"Known bugs" — kept for reference, not imported by anything).
  `reference/translated_output_example.pdf` — last known-good full pipeline
  output, useful as a visual diff baseline.

**JSON schema** (produced by extract.py, consumed/extended by later stages):
```
{
  "document": str, "page_count": int,
  "pages": [{
    "page_number": int (1-indexed), "page_index": int (0-indexed),
    "width": float, "height": float,
    "text_boxes": [{
      "id": str,                    # "p{page_index}-b{block}-l{line}-s{span}"
      "text": str,                  # raw, UNSTRIPPED (whitespace can be semantic)
      "bbox": [x0,y0,x1,y1], "origin": [x,y],   # origin = baseline point, use for reinsertion
      "font": {"name": str, "size": float, "color": int},
      "line_dir": [dx,dy],          # from the LINE not the span
      "is_rotated": bool, "rotation_angle": float,  # continuous degrees, not just 0/90/-90
      "source": "vector"|"ocr",
      "ocr": {...},                 # OCR only: xref, words_px, words_pt, group, min_conf
      "pipeline": {                 # span-level; translation lives on units now
        "classification": ..., "target_lang": str|null,
        "translated_text": str|null,  # = text for untouched spans, None inside a unit
        "needs_review": bool, "unit_id": str|absent
      }
    }],
    "segments": {"v": [[x,y0,y1]], "h": [[y,x0,x1]]},   # drawn + in-image lines
    "units": [{                     # layout_units.py
      "id", "source", "lines": [...], "paragraphs": [{"line_indexes", "text", "lead"}],
      "rect", "bounds", "size", "color", "bold", "italic", "family", "rotate", "line_height",
      "pipeline": {"classification", "target_lang", "translated_paragraphs": [str],
                   "needs_review", "scale", "overflow"}
    }]
  }]
}
```

## 3. Tech stack & conventions

- **Frontend**: Next.js 16.3.5 (App Router, Turbopack), React 19, TypeScript,
  Tailwind v4. Deployed on Vercel.
- **Backend**: standalone Python 3.x functions in top-level `api/*.py`
  (Vercel's zero-framework Python convention — `class handler(BaseHTTPRequestHandler)`
  per file, NOT FastAPI/Flask). PyMuPDF (`pymupdf`, imported as `pymupdf` not
  the deprecated `fitz` alias) for all PDF work.
- **Storage**: Vercel Blob (`woodplan-translator-blob` store, **public**
  access — see §4 for why) for PDFs in/out. Postgres (Neon, provisioned
  directly at neon.tech — see §4 for why not via Vercel Marketplace) for
  both the dynamic translation-memory cache (`glossary_terms` table) and,
  as of this session, Auth.js identity tables (`users`, `accounts`,
  `sessions`, `verification_tokens`) — same `prisma/schema.prisma`/
  migrations, same Neon database. The full `documents -> pages ->
  text_boxes` schema CLAUDE.md describes is still NOT built, only identity
  tables exist so far — see §6.
  `DATABASE_URL` (pooled) / `DIRECT_URL` (direct) are real, in `.env.local`
  and all three Vercel environments.
- **Auth**: next-auth v4.24 (`next-auth`) + `@auth/prisma-adapter` (the
  current, non-deprecated Prisma adapter package — NOT the older
  `@next-auth/prisma-adapter`), Google OAuth provider only, **database**
  session strategy. `lib/prisma.ts` is the Node-side Prisma Client
  (driver-adapter pattern, `@prisma/adapter-pg` + pooled `DATABASE_URL`) —
  entirely separate from `api/db.py`'s pg8000 connection used by the
  Python pipeline; both point at the same Neon database but are two
  independent client stacks (Node vs. Python), don't conflate them.
- **Google Drive autosave** (`feature/drive-upload`): `drive.file` scope
  requested upfront at sign-in (see `lib/auth.ts` above), refresh token
  encrypted at rest via `lib/crypto.ts` (`ENCRYPTION_KEY` env var, shared
  across all three Vercel environments). All Drive logic lives in
  Node/TypeScript (`lib/drive.ts` + `app/api/drive/*`), deliberately kept
  OUT of the Python pipeline (`api/translate.py` is unchanged) — avoids
  needing a cross-language-compatible crypto implementation, and avoids
  the risk of a compiled-extension Python Drive/crypto dependency (see
  CLAUDE.md's native-extension section). The frontend orchestrates: after
  `/api/translate` succeeds, if the toggle is on, it separately calls
  `/api/drive/save` with the resulting Blob URL -- a second, best-effort
  round trip, not something `/api/translate` itself knows or cares
  about.
- **Package manager**: npm, not pnpm. `save-exact=true` via `.npmrc`
  (project-level file — global npm config does NOT have this set, don't
  rely on it).
- **Python deps**: `requirements.txt` at repo root, pinned exact versions
  (`pymupdf==1.28.2`, `certifi==2026.7.22`, `pg8000==1.31.5`). Compiled
  packages are fine as long as `.python-version` matches the host
  `python3` and the bundle stays under Vercel's standard size (so no
  opencv) — see CLAUDE.md's Python dependencies section and §4's
  (corrected) native-extension entry. No venv checked in; Vercel's
  `uv`-based builder provisions one per function at build time.
- **Design system**: warm "drafting paper" palette (see globals.css tokens),
  Space Grotesk (display) + IBM Plex Mono (labels/mono), explicitly avoiding
  generic AI-SaaS visual patterns — see CLAUDE.md's "Design Guidelines"
  section for the full negative/positive list.
- **Git workflow**: `main` (protected) <- `dev` (integration) <- `feature/*`
  (one per feature, merged via PR — not always squashed, see below). Four
  PRs merged so far, in order: `feature/pdf-extraction` (#1),
  `feature/reinsertion-export` (#2), `feature/frontend-mockup` (#3),
  `feature/dynamic-translation` (#4, two commits: the feature itself, then
  a same-day follow-up fixing the native-extension issue in §4 — kept as
  two commits rather than amended into one, both real and informative).
  Combined commits are fine per-branch (user preference: don't split a
  feature's work across multiple commits/PRs unnecessarily) but a real
  same-day bug found via testing is worth its own commit, not silently
  folded away.
- **Testing approach**: no automated test suite exists. Verification has been
  manual-but-rigorous: run the real pipeline against `reference/sample_plan.pdf`,
  visually diff rendered PNG crops of original vs. translated pages, and for
  the frontend, drive a real headless browser (Playwright via `npx playwright`)
  against a real `vercel dev` server — not mocks. CLAUDE.md calls for "a
  handful of real unit tests for the classify/translate tokenizer and the
  rotation-aware overflow check" — **not done yet**, still open (§7).

## 4. Decisions made (non-obvious, with why)

- **Rotation is a continuous float, not a 0/90/-90 enum.** Found via testing
  that ~2% of spans in the real sample PDF are true diagonal text (angle-
  dimension callouts like "64°"), which a binary horizontal/vertical model
  would silently misplace. `extract.py` computes it via `atan2`; `reinsert.py`
  reinserts any non-zero angle via `morph=(Point(origin), Matrix(1,1).prerotate(angle))`
  rather than the `rotate=` int param, which only accepts multiples of 90.
  Verified empirically by round-tripping test angles through insert-then-
  extract before writing the real pipeline code.
- **Glossary lookup tries the RAW (unstripped) span text before a stripped
  fallback.** Some glossary entries carry meaningful leading/trailing
  whitespace because the source PDF splits one line-wrapped sentence across
  two separate text objects. `reference/pipeline_tested.py` (the original
  proof-of-concept) actually gets this wrong — it strips before ever checking
  the dict, so two of its own glossary entries can never match. Don't
  reintroduce that.
- **Glossary is whole-phrase-first, not word-by-word, for anything that isn't
  a safe standalone word.** German compounds words a English wouldn't (e.g.
  "wood" + "screw" -> "Holzschraube", one word, not "Holz Schraube"). Adding
  `"wood"` and `"screw"` as separate glossary entries would produce broken
  compounds when the tokenizer concatenates them. Only genuinely safe,
  non-compounding words got word-level entries (`"pc"` -> `"Stk."`, and the
  identity mapping `"P"` -> `"P"` for a recurring design glyph rendered as
  literal text). Everything else is a whole-span entry, even where that means
  duplicate entries for near-identical sentences (e.g. separate entries for
  "...20 pc total." vs "...16 pc total.").
- **4 spans are deliberately left untranslated** (`needs_review: true`, not
  guessed) on page 15 of the sample — a 4-line instruction ("Add spacers or
  underboard if necessary to match the Miter Saw top flat and level.") whose
  referent is genuinely ambiguous and is functional assembly guidance, not
  decorative copy. Don't "fix" this by guessing a translation without
  re-reading the source PDF page yourself first.
- **PDFs move through Vercel Blob, not function request/response bodies.**
  Vercel Functions cap both directions at 4.5MB; real plan PDFs (sample is
  15MB, translated output ~16.6MB) blow through that 3-4x. This is why the
  upload flow is: browser -> Blob directly (via `@vercel/blob/client`) ->
  `api/translate.py` downloads from Blob -> processes -> uploads result to
  Blob -> returns a URL. Not a raw file upload to the function.
- **The Blob store is public access, not private.** Vercel Blob enforces
  access mode per-*store*, not per-object — you cannot mix public and private
  blobs in one store (confirmed by hitting `"Cannot use public access on a
  private store"` in production). The original design intent was private
  source uploads + public translated output, which isn't possible without
  either two separate stores or presigned-URL signing (no official Python
  SDK support for that, would need hand-rolled HMAC/signature logic). Given
  there's no auth/ownership system yet anyway, went with one public store;
  protection today is purely an unguessable random-suffix pathname. **Revisit
  once `feature/auth`/`feature/db-schema` exist** — presigned URLs or a
  private store scoped per-user would be the correct long-term answer.
- **LLM fallback (Gemini) only fires on whole-span misses, never on
  per-token/mixed-span misses.** Per CLAUDE.md's translation-engine
  priority order (glossary first, LLM fallback second), but scoped
  narrowly: sending individual words to an LLM for translation would
  reintroduce the exact homograph problem ("board" lumber vs. ship) that
  full-sentence context exists to solve. A mixed span's uncovered word
  token just stays untranslated/flagged, same as before this feature.
- **The dynamic translation cache (`glossary_terms`) is global/unscoped,
  not per-seller**, even though multi-tenant auth is planned
  (`feature/auth`). Safe by construction: caching is exact-match only (no
  fuzzy/embedding search), so one seller's text can never accidentally
  match another's — different string, different key. Sharing the cache
  across all future sellers is a pure benefit (more reuse of generic
  woodworking vocabulary); add a nullable `seller_id` column later, as a
  non-breaking migration, only if per-seller isolation is ever actually
  needed.
- **Gemini rate limiting is in-process only** (batch a whole document's
  misses into as few calls as possible, pace sequential calls within one
  request, backoff on 429/5xx) — no shared/distributed limiter (e.g.
  Upstash Redis) across concurrent invocations. Deliberate: there's no
  auth/multi-tenancy yet, so no real concurrent load to coordinate
  against; adding that infra now would be solving a problem that doesn't
  exist. Revisit once `feature/auth` makes concurrent usage real (see §7).
- **Neon Postgres was provisioned by signing up directly at neon.tech,
  NOT via `vercel integration add neon`.** Vercel's marketplace-managed
  billing wrapper for Neon reportedly has no hard spend cap once free-tier
  usage is exceeded (per Vercel's own community forum), whereas Neon's own
  direct free tier (100 CU-hours/month, 0.5GB) is hard-capped with no card
  on file — the safer choice for an MVP with an explicit zero-spend goal.
  No Neon Auth, no Neon CLI/MCP/config-as-code setup — none of that is
  needed for a single Postgres table, and Neon Auth would conflict with
  the already-decided NextAuth/Google-OAuth plan.
- **No Python dependency with a compiled/native extension in `api/*.py`,
  even if it works fine in a local venv.** `google-genai` (-> `pydantic`/
  `pydantic-core`, compiled Rust) and `psycopg[binary]` (-> compiled
  `libpq`/`psycopg_c`) both installed and imported cleanly in a plain
  local venv, then crashed **every single request** on real `vercel dev`
  with `NO_RESPONSE_FROM_FUNCTION` (`ModuleNotFoundError: No module named
  'pydantic_core._pydantic_core'`, then `ImportError: no pq wrapper
  available`) — a platform mismatch between the built wheel and Vercel's
  Python function runtime. This is a known, recurring, *unresolved* issue
  for these specific packages on Vercel (per Vercel's community forum and
  pydantic's own GitHub issues), not something specific to this repo, and
  it can also bite real deployments, not just local `vercel dev`
  emulation. Fixed by switching to pure-Python alternatives: raw REST via
  `urllib` for Gemini (matching `api/blob.py`'s existing hand-rolled
  pattern) and `pg8000` for Postgres. **Before adding any new Python
  dependency, check whether it ships a compiled extension, and confirm it
  works via a real `vercel dev` request** (clear `.vercel/cache` and
  `api/__pycache__` first, or a stale build keeps serving old code) —
  don't trust a clean local venv install alone.
  - **Update 2026-09-21 — the root cause above was misdiagnosed; this
    was a local Python-version mismatch, not a Vercel platform issue.**
    Reproduced with numpy: `@vercel/python` BUILDS deps with its
    default Python (3.12, since the repo has no `.python-version`) but
    `vercel dev` RUNS the function with the host's plain `python3`
    (Homebrew 3.14 here — `pythonPath: "python3"` in
    `@vercel/python/dist/index.js`). Per-Python-version wheels (numpy,
    pydantic-core, psycopg) then fail with exactly the errors above
    (`_multiarray_umath.cpython-312-darwin.so` incompatible with
    cpython-314); PyMuPDF never broke only because its wheel is `abi3`.
    Adding `.python-version` = `3.14` made numpy import and run cleanly
    under real `vercel dev` (probe returned 200), and **confirmed on a
    real Vercel Preview deploy** (Linux x86_64, Python 3.14.7, numpy
    2.5.3): numpy-only inpaint erased 100% of test ink, `replace_image`
    + `insert_htmlbox` worked, existing `/api/extract` and the homepage
    still loaded under 3.14. Deployed via `vercel deploy` from a
    throwaway git worktree (no branch pushed), then removed. The
    rebuild branch should commit `.python-version` = `3.14` (keeps
    local build/run Python identical) plus `numpy==2.5.3`.
  - **Separate hard limit: opencv is out.** `opencv-python-headless`
    (4.x and 5.x alike, `cv2` ~125MB unpacked) pushes EVERY Python
    function past Vercel's standard bundle size (249MB), which switches
    it to runtime dependency install via a Linux `uv` binary — `vercel
    dev` on macOS then fails with `Exec format error` for all of
    `api/*.py`, including `/api/translate`, since `requirements.txt` is
    shared. numpy alone (22MB) stays under the limit.
- **Reference rebuild target: `working python script/pdf_translate.py`**
  (user's standalone script, 2026-09-21, produces clean DE/ES/FR output
  on the real sample PDFs). Compared against this pipeline; it avoids
  our render bugs through four structural differences: (1) paragraph
  units (spans→lines→clustered paragraphs, de-hyphenated) instead of
  per-span translate/fit; (2) `insert_htmlbox` with `scale_low` (wrap
  then uniform shrink, returns spare/scale) instead of `insert_text` +
  the shrink/wrap/bleed tiers; (3) real obstacle detection (drawn line
  segments, other text, placed boxes, a 72-dpi `PixelProbe` for diagram
  ink) instead of bbox-width guesses; (4) glyph-only redaction
  (`fill=False`, inset rect, images/line-art NONE) of translated lines
  only, and OCR text erased by inpainting the embedded image +
  `replace_image` instead of white rects. Its gaps: sends numbers
  through Gemini (10/754 cached entries had changed digits/units),
  `gemini-2.5-flash` + 40-item chunks + no glossary/Neon cache, opencv
  dependency (see above — replace `cv2.inpaint` with a numpy diffusion
  fill; probe erased 100% of test ink in 58ms), large output files.
  Our OCR font size (`_HEIGHT_TO_FONT_SIZE_RATIO = 0.75`) also looks
  ~30% too small vs the script's `h/0.72`–`h/0.92` estimate (from
  reading the code, not measured).
- **`package.json`'s `dev` script stays `next dev`, not `vercel dev`.**
  `vercel dev` internally shells out to `npm run dev` to start the framework
  dev server — pointing `dev` at `vercel dev` itself causes infinite-
  recursion errors. The full-stack-including-Python-functions dev command is
  `npm run dev:full` (= `vercel dev`). **`npm run dev` alone does NOT serve
  `api/*.py` at all** — hitting `/api/translate` under plain `next dev`
  returns Next.js's HTML 404 page, which breaks frontend `res.json()` calls
  with `"Unexpected token '<'"`. This is the single most likely thing to trip
  up a fresh session — see §7.
- **PyMuPDF's `apply_redactions()` default `images=2` ("blank out
  overlapping image parts") is not safe to leave at its default in this
  pipeline.** Found by pixel-diffing rendered output against
  `reference/sample_plan_2.pdf`: any text span whose redaction rect merely
  *touched* a diagram's raster image bounding box (not necessarily over
  visible diagram ink) got a visible boundary line baked permanently onto
  the page. Isolated by toggling `apply_redactions`'s `images=`/`graphics=`
  params independently against real sample pages and diffing pixel values
  at the artifact's exact location — `images=0`/`images=1` never showed
  it, `images=2` always did, `cross_out` on `add_redact_annot` made no
  difference either way. `reinsert.py` now always passes `images=0`: this
  pipeline's own rule is that diagrams are never touched, so there's no
  case where `images=2`'s blanking behavior should have been wanted here
  in the first place.

## 5. What's done

- Full pipeline: extract -> classify/translate (4 tiers, including the
  Postgres-cache + Gemini fallback) -> refit/reinsert/export. Verified
  against all 15 pages / 533 spans of the real sample PDF for all three
  languages (German, Spanish, French), with real Gemini API calls and a
  real Neon database (not mocked), including a real end-to-end request
  through `vercel dev` (upload to Blob -> `/api/translate` -> download and
  render the result) — not just direct Python calls in a venv.
- German glossary: 114 static entries, covers all but a handful of spans
  of the sample PDF; those remaining spans now correctly resolve via the
  Gemini fallback tier instead of staying flagged. Spanish/French have
  ~0 static entries so far — nearly everything routes through Gemini for
  those two today, and the dynamic cache is expected to fill that gap
  from real usage (see §4).
- Dynamic translation cache confirmed actually working, not just present:
  a second pipeline run with the Gemini call monkey-patched to raise if
  invoked at all produced identical results — cache hits genuinely skip
  the API rather than coincidentally matching.
- Frontend: upload/translate/download flow with a DE/ES/FR language
  selector, no login, verified end-to-end through a real browser against
  real `vercel dev` + real Vercel Blob. `npm run build` and `npm run lint`
  both pass clean.
- Vercel Blob store provisioned and connected (`woodplan-translator-blob`,
  public, static `BLOB_READ_WRITE_TOKEN` + OIDC both available). Neon
  Postgres provisioned and connected (`glossary_terms` table, plus the
  Auth.js identity tables as of this session — see §4 for why the
  glossary cache is global/unscoped and why Neon was signed up directly
  at neon.tech).
  `GEMINI_API_KEY` (Google AI Studio free tier) live in `.env.local` and
  all three Vercel environments.
- Six PRs merged to `dev`: #1 (extraction), #2 (reinsertion/export +
  glossary expansion), #3 (frontend), #4 (dynamic Gemini translation +
  Spanish/French + the native-extension fix in §4), #5
  (reinsert/glossary render-bug fixes), #6 (user database + Google auth +
  the postinstall build fix).
- Fixed several visible rendering bugs in `reinsert.py`/`glossary.py`,
  all root-caused against real output from `reference/sample_plan.pdf`,
  `reference/sample_plan_2.pdf`, and broken-output repros the user
  generated locally (`reference/sample_plan_broken_{de,es}.pdf`,
  `reference/sample_plan_2_broken.pdf`) — merged to `dev` via PR #5
  (`feature/reinsert-render-fixes`). Those four extra PDFs ended up
  **gitignored, not committed** (`88ed323`) — kept locally as scratch
  repros, not checked in as permanent fixtures; only the original
  `reference/sample_plan.pdf` and `reference/translated_output_example.pdf`
  are tracked in git:
  1. A rectangular border baked onto the page wherever a reinserted
     span's redaction rect touched a diagram's raster image, root-caused
     to `apply_redactions()`'s default `images=2`; fixed by passing
     `images=0` (redaction must never touch image content here at all,
     per this module's own "never touch the diagram" rule).
  2. Translated text hardcoded to Helvetica regardless of the span's real
     font; fixed by mapping the extracted font name to the closest
     base-14 family/weight (`_base14_font`).
  3. Genuinely-overflowing spans (still too long even at the normal
     75-80% shrink floor) rendered unclipped, bleeding sideways across
     their column border into neighboring cells or wrapping down into
     whatever sat directly below (often a diagram, not blank paper).
     Fixed with a 3-tier fallback in `_insert_span`, tried in order: (a)
     `_deep_shrink_fit` — shrink further, well past the normal floor
     (down to `_DEEP_SHRINK_FLOOR_RATIO`/`_DEEP_SHRINK_MIN_SIZE`), to see
     if the whole translation fits back on its ORIGINAL single line; (b)
     a bbox-width-capped, sibling-line-aware `insert_textbox` wrap for
     text tier (a) can't rescue; (c) the original single-unclipped-line
     render as a last resort. Scanning every page of the real sample PDF
     found 12 real German overflow cases — tier (a) alone now resolves
     all 12, so tiers (b)/(c) are no longer hit by any case found in real
     data so far (kept as fallbacks for content tier (a) genuinely can't
     fit, e.g. a whole extra sentence's worth).
  4. One static glossary entry pair (`"using paint or stain. Keep tools
     and "` / `"materials out of reach of children."`) had the German
     translation split very unevenly between the two original line-spans
     (almost everything on line 2's much narrower box), which even
     tier-(a) shrink alone rendered inconsistently-sized between the two
     lines. Rebalanced the word split between the two glossary entries
     (moved "Material" onto line 1) using each line's real bbox width
     from the sample PDF — both lines now need a similar, moderate
     shrink instead of one full-size and one drastically tiny.
  All four originally-reported real cases (D1-A/D1-B captions overlapping
  the diagram in `sample_plan_2`; the German safety-notice text crossing
  the dashed layout-guide line; the Spanish title bleeding into the
  copyright column; "Cerrada" splitting mid-word) are now confirmed fixed
  by re-rendering the exact same real spans through the current code.
- User database + Google authentication (this session, on
  `feature/google-auth`, branched off `dev`): Auth.js identity tables
  (`users`/`accounts`/`sessions`/`verification_tokens`) added to
  `prisma/schema.prisma` and migrated onto the real Neon database
  (migration `20260919174602_add_auth_tables`, applied via
  `DIRECT_URL`/`prisma7.config.ts`); `lib/prisma.ts` + `lib/auth.ts` +
  `app/api/auth/[...nextauth]/route.ts` wire next-auth v4 +
  `@auth/prisma-adapter` + Google provider (database session strategy);
  `app/providers.tsx` + `app/components/AuthStatus.tsx` add a real
  sign-in/sign-out UI (top-right bar in `layout.tsx`). `npm run build`
  and `npm run lint` both pass clean.
  - **Verified against a real browser, not just a type-check**: started
    `next dev`, drove headless Chromium (via a cached `npx playwright`
    install — `chromium-cli` wasn't available in this environment) to
    click "Sign in with Google", and confirmed the redirect actually
    reaches Google with the real `client_id` from `.env.local` — Google's
    own response was `Error 400: redirect_uri_mismatch` for
    `http://localhost:3000/api/auth/callback/google`, which proves the
    credentials are real (not placeholders, contra the previous
    entry's assumption) and the whole provider/redirect wiring on this
    end is correct. The mismatch itself is a one-time fix only the user
    can do (their Google Cloud Console project) — see §7.
  - Scope is intentionally narrow: identity/login only, no
    `drive.file`/offline-access scope requested yet (so no
    `refresh_token` is issued or stored yet — encrypting that column at
    rest per CLAUDE.md is deferred until the Drive-upload feature
    actually populates it), and no route/feature is gated behind
    sign-in — the upload/translate flow on `/` still works fully
    signed-out, unchanged. This session was "get identity + DB wired",
    not "require login to use the tool."
  - Committed (`869eb68`), pushed, and **merged to `dev` via PR #6**.
- Fixed a Vercel build failure on `feature/google-auth`, found right
  after the user pushed and a Preview deploy errored: Turbopack failed
  with `Module not found: Can't resolve '@/app/generated/prisma/client'`
  in `lib/prisma.ts`. Root cause: `app/generated/prisma` is correctly
  gitignored (it's generated code), but nothing regenerated it on
  Vercel's fresh clone before `next build` ran. Fixed by adding
  `"postinstall": "prisma generate"` to `package.json` — verified for
  real by deleting `app/generated` locally, running `npm install`
  (confirmed `postinstall` fired and regenerated it), then `npm run
  build` clean, same as what Vercel's pipeline does. Committed as
  `b73b03a`, pushed directly to `feature/google-auth` (already merged
  into that PR before #6 landed).
- Found (via the Vercel API's `get_project`, not the CLI, which doesn't
  surface this) that **Vercel Authentication (SSO) deployment protection
  is enabled project-wide, `deploymentType: "all_except_custom_domains"`**
  — since this project has no custom domain yet, this covers Production
  too, not just Preview. Flagged as a real landmine for later (any real
  visitor without Vercel-team access would hit Vercel's own login wall
  before reaching the app at all) but NOT changed — no custom domain
  exists yet to make disabling it obviously safe, and it wasn't asked
  for. Revisit once a custom domain is attached to Production, or
  earlier if this blocks real user testing.
- Found + the user fixed a corrupted `NEXTAUTH_URL` value for the
  Preview environment: after the `dev`-branch Preview deployment came
  up, Google's own OAuth error page showed
  `redirect_uri=https://woodplan-translator-mvp-git-dev-....vercel.app,/api/auth/callback/google`
  — note the **comma** right before the path, meaning the env var's
  actual stored value had a trailing comma appended
  (`...vercel.app,` instead of `...vercel.app`), making the whole
  constructed `redirect_uri` malformed — Google rejected it with `Error
  400: invalid_request`, a different failure mode than the earlier
  `redirect_uri_mismatch` (that one was a registration gap; this one was
  a corrupted stored value). The comma was visible directly in Google's
  error response, no secret-value inspection needed. User removed and
  re-added `NEXTAUTH_URL` for Preview and triggered a redeploy (confirmed
  via `vercel ls`/`vercel inspect`: a fresh Preview deployment exists on
  the `git-dev` alias, created shortly after the fix was reported done).
  **User has since confirmed Google sign-in completes on the `dev`
  Preview deployment** — reported directly by the user after testing in
  their own (Vercel-authenticated) browser, not independently
  re-verified via an automated browser test on this end (still can't —
  the Vercel SSO wall from the previous bullet blocks a session-less
  headless browser before it ever reaches the app). Treated as reliable
  since it's a direct first-hand report of the actual thing that
  matters (a completed sign-in), not an inference from config.
- Google Drive autosave built end-to-end on `feature/drive-upload`
  (branched off `dev`): schema migration (`driveAutosaveEnabled`,
  `driveFolderId` on `User`), `lib/crypto.ts`, `lib/drive.ts`, two new
  API routes, `lib/auth.ts` scope + encryption changes (including the
  `events.signIn` fix for already-linked accounts, see the Auth entry
  above in §2), and a checkbox toggle + post-translate save call in
  `app/page.tsx`. `npm run build` and `npm run lint` both pass clean.
  - Merged to `dev` via PR #8, alongside a new `/privacy` page (linked
    from a site-wide footer) written for Google's OAuth consent screen
    verification.
  - **User has since confirmed a real signed-in end-to-end test**: Drive
    API enabled in Google Cloud Console, `drive.file` added to the OAuth
    consent screen's scopes, tested while the app is still in Google's
    Testing publishing status (their own account as a test user) — sign
    in with Drive permission granted, toggle on, translate, and the file
    actually landed in the Drive folder. Reported directly by the user,
    not independently re-verified via an automated test on this end
    (same limitation as Google sign-in itself — no way to complete
    Google's real consent screen from a session-less browser here).
    Treated as reliable for the same reason those earlier reports were:
    it's a first-hand report of the actual thing that matters (a real
    file landing in Drive), not an inference from config.
  - `ENCRYPTION_KEY` (fresh 32-byte value, generated this session via
    `openssl rand -base64 32`) added to `.env.local` and all three Vercel
    environments (shared value, same pattern as `NEXTAUTH_SECRET`).
- Fixed a real, user-reported font-size-consistency bug in
  `reinsert.py`: translated text within what should be one uniform
  vertical list, or one multi-line heading, rendered at wildly
  inconsistent sizes (some tiny, some full-size, no visible pattern).
  Root-caused against real extracted span data before touching any code
  (user explicitly asked for that order): `_available_length` measured
  a span's "room to grow" as its own original bbox width — exactly as
  wide as the ENGLISH word happened to render, not a real layout
  boundary. A Tools checklist where every original item is genuinely
  20pt confirmed this exactly: "Jig Saw" (short original, ~72pt of
  inherited slack) -> "Scie sauteuse" crushed to 12.6pt, while "Safety
  goggles and gloves" (long original, ~227pt of slack) stayed full
  20pt, purely because the English words differed in length. Fixed by
  loosening the constraint (real page margin, capped to a bounded
  multiple of the original width) for spans with no real same-line
  neighbor, added as new logic in `_available_length`
  (`_has_same_line_neighbor` is the safety gate) — see `reinsert.py`'s
  own module docstring for the full writeup, including the one blind
  spot (extract.py doesn't capture diagram image bboxes, so "no text
  neighbor" can't fully rule out a diagram occupying the loosened
  space — bounded via a width multiplier rather than trusted all the
  way to the page edge). Verified against real data, not just theory:
  the exact reported Tools-list case now renders fully uniform at 20pt
  with zero shrink in both Spanish and French; a full 15-page German
  re-run of the whole pipeline shows no regression on dimension-heavy,
  diagram-adjacent pages (rendered to PNG and visually checked); 218
  real spans with a genuine same-line neighbor confirmed to get the
  exact prior tight behavior, unchanged.
  - Committed, pushed, merged to `dev` and then `main` (PR #10) —
    confirmed `READY`/`target: "production"` via the Vercel API.
- Google Vision OCR fallback extraction built on `feature/ocr-fallback`
  (branched off `dev`): flatness detection in `extract.py`, the new
  `ocr_vision.py`/`geometry.py` modules, and the OCR-erasure branch in
  `reinsert.py` -- see §2's architecture entries above for each piece's
  own writeup.
  - **Verified against the REAL Vision API, not just mocked responses**
    -- the user provided `GOOGLE_VISION_API_KEY` (after first creating a
    service-account key by mistake; talked through why that specific
    credential type was avoided on purpose -- it needs JWT signing,
    which needs a compiled crypto library, exactly the risk this whole
    module was designed to route around). Ran the full
    flatness-detection -> real Vision OCR call -> classify/translate
    (real glossary + Gemini, real German output) -> reinsert -> render
    pipeline against the real synthetic flattened Tools-checklist page,
    end to end, more than once as bugs were found and fixed.
  - **Two more real bugs found from the REAL Vision response that the
    earlier mocked tests could not have caught, since they were built
    from idealized geometry**:
    1. Vision's real word bounding boxes carry several degrees of
       detection jitter even for genuinely horizontal text (confirmed
       directly: up to 6.34° on an entirely horizontal real page) --
       the original 0.5° rotation epsilon (tuned for PyMuPDF's
       mathematically-exact vector line directions) flagged roughly a
       third of that page's real OCR lines as `is_rotated: True` purely
       from noise, which matters because reinsert.py's rotated-text path
       skips the entire overflow/shrink system. Fixed by giving
       `geometry.rotation_angle` a configurable epsilon
       (`_OCR_ROTATION_EPSILON_DEGREES = 8.0` in `ocr_vision.py`,
       comfortably above the observed noise, still well below any real
       diagram angle-callout seen in vector data so far) rather than
       widening the shared default used by the precise vector path too.
    2. The erasure padding (`_OCR_ERASE_PADDING`), tuned to 0.75pt
       against a synthetic test using a real vector span's own already-
       generous bbox, was too optimistic for Vision's real (tighter)
       boxes -- found by zooming into the real rendered output at each
       step: a sliver of "Tools:" was still visible above its reported
       top edge at 0.75pt; a "p" descender (from "Tips" in "Safety
       Tips :") was still visible below its reported bottom edge even
       at 3.0pt. Settled on 5.0pt after re-verifying the exact same
       regions clean at each step -- see `reinsert.py`'s own comment for
       the full before/after.
  - **A real gap found by the user testing a real seller PDF
    (`reference/sample_with_raft_img.pdf`, 55 pages, gitignored like the
    project's other secondary fixtures -- kept local, not committed)
    that the synthetic test fixture structurally could not surface**:
    pages with real
    vector text AND a separate embedded image with its OWN baked-in
    text (e.g. a full 20+ row materials/cost table, baked entirely into
    one image) were silently completely untouched -- the whole-page
    flatness fallback correctly never triggers for such a page (it does
    have real text), and nothing else was checking individual embedded
    images at all. Added `ocr_image_regions` to `ocr_vision.py`, wired
    into `extract_document` for exactly this case (real text present +
    at least one embedded image above a minimum size): OCRs each image
    individually and ADDS any text found to that page's existing
    vector-extracted text_boxes.
    - **A second real bug found immediately by testing against that
      same real PDF, not caught in dev**: the first implementation
      OCR'd each image via a `get_pixmap(clip=image_bbox)` page-region
      render, same mechanism as the whole-page case. That's wrong for
      THIS case specifically: rendering a page region also renders any
      REAL VECTOR TEXT positioned on top of or near that image, so
      Vision re-detected text ALREADY correctly captured by normal
      vector extraction -- both the real vector span and the spurious
      OCR duplicate got drawn, independently, at slightly different
      positions, producing literal doubled/ghosted text in the real
      rendered output (e.g. two overlapping "4335 mm" labels). Confirmed
      by cross-checking the real page's own vector text dump: every
      single "duplicated" label was already a real vector span.
      Rewrote `ocr_image_regions` to OCR each embedded image's OWN RAW
      PIXEL DATA instead, extracted directly via its PDF xref
      (`_ocr_raw_image`) -- immune to this by construction, since a raw
      image file has no vector content at all. Re-verified against the
      same real page: the raw images turned out to have ZERO baked-in
      text at all (every visible label really was vector text, which is
      why the page looked doubled rather than simply having extra
      content) -- the fix produces a byte-for-byte-equivalent-looking
      clean render now, confirmed visually. Also added overlap-based
      deduplication (`_MAX_IMAGE_OVERLAP_RATIO`) as a secondary
      safeguard for the different case of two embedded images
      genuinely, substantially overlapping each other in page space
      (also found on this same real PDF -- a "zoomed detail" callout
      image overlapping three of the page's other four images).
  - Not yet committed as of this latest real-PDF verification pass — see §6.

## 6. What's in progress

**Layout rebuild DONE and SHIPPED to Production (2026-09-22).**
`feature/layout-rebuild` (cut from `feature/ocr-fallback`, since the OCR
modules it rewrites existed only there) merged to `dev` via PR #12, then
`dev` merged to `main` via PR #13 and deployed — confirmed `READY` for
commit `b13945d` via the Vercel API, plus a live check against the
deployed site (`/` 200, `/api/extract` 501 as expected for a GET). This
also carries the earlier `feature/ocr-fallback` commits (whole-page +
per-image OCR) to Production for the first time. Rebuilt the pipeline
around `working python script/pdf_translate.py`'s
approach (see §2 for each module), keeping our glossary / Neon cache /
`gemini-3.1-flash-lite` batching, pure-numpy instead of opencv, plus
`.python-version` = 3.14 and `numpy==2.5.3`.
- Step 1 (vector layout): units, masking, glyph-only redaction,
  obstacle-aware `insert_htmlbox` layout. Beyond the script: styled
  lead-in codes keep their weight, callout labels grow left, duplicate
  lines merged, invisible text skipped, glossary codes like "D1-A."
  handled without Gemini, ALL-CAPS kept on glossary hits.
- Step 2 (OCR): Vision once per image per document; lines split on large
  gaps (table columns) and grouped by Vision paragraph; font size and
  baseline measured from the INK rows (Vision's word boxes vary 10px vs
  13-16px for same-size text, which rendered rows 50% too large);
  text colour sampled from pixels; OCR lines overlapping vector text /
  conf < 0.6 / < 4pt dropped; rotated/mirrored image placements skipped;
  straight lines inside images detected (numpy) and used as borders
  (fixed a title spilling out of an in-image frame); erasure by numpy
  inpainting + `replace_image` instead of white rects; single-line table
  cells never grow down.
- Verified, not just direct calls: sample_plan_2 (DE) vs the script's
  output — equal or better on every page, 0 overflows; sample_plan (15
  pages, DE + ES) — every previously reported layout bug gone, DE 196/212
  units from the glossary; sample_with_raft_img pages 3/4/5/55 (ES, real
  Vision) vs the script — materials table inpainted cleanly, uniform
  size, rows aligned with the untouched numbers, and MORE correct than
  the script ("1 set" stays original pixels; the script printed "Yo
  instalo" from Vision's "I set" misread), output 1.2MB vs the script's
  4.3MB for the same pages. Real `vercel dev` -> `/api/translate`
  requests with Blob upload/download for both the vector sample and the
  OCR pages: output pixel-identical to the direct runs.
- Known remaining: very narrow cells/labels still shrink below 0.75
  (flagged needs_review), e.g. "4 pc" between two leader dots, "Interior
  Doors" in a narrow OCR table cell; Gemini occasionally puts a
  placeholder in an odd position ("⟦0⟧ doors" -> "Puertas 3"); brand
  text like "DIY Plans" gets translated (the script had a `--keep` list;
  we have none yet); diagonal-angle OCR units are left untranslated.
- Test-run cache rows written with an early masking bug were deleted
  from `glossary_terms` (only rows created by those runs). The table
  holds ~300 de / ~570 es / ~200 fr rows, not the "4 German rows" §7
  mentions below.

Also still open, unrelated to the above: **no genuinely
scanner-originated (camera/scan artifacts) test PDF has been tried** --
see §7 for the current status of what has and hasn't been covered.

User is now working through Google's publishing/verification flow to
move the OAuth consent screen out of Testing status (submitting the new
`/privacy` page's URL as part of that). This is entirely a Google Cloud
Console process outside this repo — nothing to track here beyond
noting it's in progress. Since `drive.file` is a sensitive scope, this
will likely trigger Google's verification review (homepage URL, a
justification per sensitive scope, possibly a demo video), not just a
status flip — offer to draft the scope-justification text if asked, the
exact `drive.file` usage is well understood from building the feature.

Otherwise nothing else mid-edit. User/Google-auth work from earlier is
fully closed out: `feature/google-auth` merged to `dev` (PR #6), `dev`
merged to `main`, deployed to Production (confirmed `READY`/`target:
"production"` via the Vercel API), Vercel SSO deployment protection
resolved (see §7 for the current state and one side effect worth knowing
about), and Google sign-in confirmed working end-to-end on all three
environments — Production, `dev` Preview, and `localhost:3000`.

Env var provisioning in Vercel: done and verified via `vercel env ls`
(see CLAUDE.md's Vercel env var subsection for the gotchas hit getting
here — multi-environment `vercel env add` silently sharing one value,
`vercel env pull` masking Secret values, a stray comma corrupting a
value). `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`NEXTAUTH_SECRET` share
one value across Production+Preview+Development; `NEXTAUTH_URL` is three
separate correct values, one per environment.

The natural next slice, per CLAUDE.md's suggested branch order:
- Extending the schema beyond identity tables — the full `documents ->
  pages -> text_boxes` hierarchy CLAUDE.md originally described is still
  not started. Now that `users` exists for real, this is unblocked, but
  wasn't asked for in this session — don't build it speculatively.
- `feature/editor-mvp` — explicitly deprioritized ("skip the editor for
  now, as it's an extra feature") in favor of the no-login frontend.
  Fabric.js/Konva.js canvas, not started at all.
- Whether to actually gate any part of the app behind sign-in (currently
  nothing is) is an open product decision, not yet made.

## 7. Known issues / TODOs

- **ROOT CAUSE FOUND (2026-09-21) for the localhost OCR report below:**
  under `vercel dev`, the Python functions do NOT see
  `GOOGLE_VISION_API_KEY` — a real `/api/translate` request logged
  `RuntimeError('GOOGLE_VISION_API_KEY is not set')` for every page,
  while Gemini/DB worked. The key exists only in `.env.local` (39 chars)
  and has zero rows in the Vercel project env (`vercel env ls`);
  `GEMINI_API_KEY`/`DATABASE_URL` work because they ARE in Vercel's
  Development env. So `vercel dev` Python functions evidently get the
  Vercel project's env, not `.env.local`. **Fixed 2026-09-21:** added
  via `vercel env add GOOGLE_VISION_API_KEY <env> --sensitive`, one call
  per environment (development/preview/production, same value, piped
  from `.env.local`). Re-ran the real `vercel dev` -> `/api/translate`
  request: zero "not set" errors, request time 32s -> 83s (Vision calls
  now happening). The OCR failure is caught per-page, which is why it
  looked like "OCR does nothing" rather than an error. Rule of thumb:
  any new key a Python function needs must be in Vercel's env, not
  just `.env.local`, even for local `vercel dev`.
- ~~**OCR still does not work when actually tested through localhost**~~
  — **resolved**, see the root-cause entry directly above (missing
  Vercel env var) and §6 (OCR rebuilt and verified through real
  `vercel dev`). Original notes kept below for context: After the
  per-image-OCR + raw-image-extraction fix (commit `c4ae952`), the user
  reported it's still broken when tested for real on `localhost:3000`,
  despite that fix being verified via direct Python calls into
  `extract`/`classify_translate`/`reinsert` in a venv (not through the
  actual running app). Session ended here at the user's request (tired,
  explicitly asked to stop and just log this) — genuinely not
  diagnosed, not even reproduced through the real app yet. Concrete
  things to check first, before assuming it's the same class of bug as
  before:
  - **What exactly fails, and how** — no error text, screenshot, or
    description of the failure mode was captured this session. Ask, or
    reproduce directly, before guessing.
  - **Which dev command was running.** This project's own documented
    gotcha (§8, CLAUDE.md): plain `npm run dev` does NOT serve
    `api/*.py` at all — hitting any Python endpoint under it 404s with
    Next.js's HTML page, not a Python error. `npm run dev:full` (`vercel
    dev`) is required for `api/translate.py` to run at all locally. If
    the user was running plain `npm run dev`, that alone would look
    exactly like "OCR doesn't work" (translation of anything wouldn't
    work, not just OCR specifically) — worth ruling out first, it's the
    cheapest possible explanation.
  - **Stale Python build cache.** Also already-documented (§8): `vercel
    dev` can silently keep serving a previous build of an `api/*.py`
    function after a source change, unless `.vercel/cache` and
    `api/__pycache__` are cleared first. The OCR modules changed
    materially this session (new files even) — a stale build silently
    still running pre-fix code would look identical to "the fix didn't
    work."
  - **Whether `GOOGLE_VISION_API_KEY` is actually reaching the Python
    function's environment under `vercel dev`**, not just present in
    `.env.local` — `vercel dev` reads env vars differently than a
    directly-invoked Python script in a venv does; the direct-call
    verification this session bypassed that whole path entirely, so it
    proves the pipeline LOGIC is right but proves nothing about whether
    `vercel dev` itself is wiring the key through correctly.
  - Only after ruling out the above: re-open the actual code path
    (`api/translate.py` -> `extract.extract_document` ->
    `ocr_vision.py`) for a real bug, with real error output from a real
    `vercel dev` request in hand, the same way every other real bug this
    session was root-caused -- not by re-running the same direct-call
    verification that already passed once and evidently isn't the whole
    story.
- **Google Vision OCR fallback is not rate-limited or cost-tracked at
  all — and the real cost exposure is now confirmed worse than
  originally estimated.** Unlike everything else in this stack (Neon's
  hard-capped free tier, Gemini's in-process rate limiting per
  CLAUDE.md's own deliberate design), Cloud Vision's
  `DOCUMENT_TEXT_DETECTION` has a real, fairly modest free tier
  (historically ~1000 units/month, 1 request = 1 unit, then billed past
  that). The original estimate assumed "1 call per flattened page," but
  `reference/sample_with_raft_img.pdf` (real seller PDF, 55 pages)
  showed a single page can have 4 embedded images, several large enough
  to pass the size filter — meaning a single MIXED page (not even a
  flattened one) can cost multiple Vision calls, and a real multi-page
  image-heavy document could burn through the monthly free tier in a
  single translation request. Still not addressed (out of scope for
  what was asked) — a hard per-document call budget is worth doing
  before this sees real, unsupervised traffic, more urgently than
  originally scoped.
- ~~`GOOGLE_VISION_API_KEY` only in `.env.local`~~ — **fixed**: now in
  all three Vercel environments (see the root-cause entry above).
- **No real SCANNED (camera/scanner-originated) test PDF exists yet** —
  everything verified so far is either a synthetic flattened fixture (a
  real page's real vector text re-rendered as one image) or a real
  born-digital seller PDF with embedded images
  (`sample_with_raft_img.pdf`, confirmed to have zero real scan
  artifacts — its images are clean exports, not photographs of paper).
  This has exercised the real Vision REST contract, real OCR-detection
  geometry, and a real multi-image mixed-page layout — genuinely
  thorough coverage now — but still never a real scanner's actual skew,
  noise, or compression artifacts. Lower priority than it was before
  this session's real-PDF testing, but still worth doing if a customer
  ever uploads an actual scanned plan.
- **Google Drive autosave now confirmed working end-to-end by the user**
  (see §5) while the OAuth consent screen is still in Google's Testing
  publishing status — Drive API enabled, `drive.file` added to the
  consent screen's scopes, real sign-in -> toggle -> translate -> file
  landed in Drive. **Still only confirmed for the developer's own
  account** (the one on the Testing status's test-user list) — anyone
  else's account will be blocked at Google's consent screen until the
  app moves to "In production" publishing status, which for a sensitive
  scope like `drive.file` means going through Google's verification
  review, not just a status flip. That's in progress (see §6) — not
  done as of this entry, don't assume any account beyond the
  developer's own can use this feature yet.
- **No handling yet for a revoked/expired Drive refresh token** (e.g.
  the user revokes access from their Google Account settings, entirely
  outside this app). `saveTranslationToDrive` would fail at the
  token-refresh step with whatever error Google's token endpoint
  returns; `/api/drive/save` catches it and returns a generic error, and
  the frontend just shows "Drive save failed" with no specific
  "reconnect your Drive access" guidance or re-auth prompt. Low priority
  for MVP (the download button still works regardless), but a real gap
  if this ever gets real usage.
- **Vercel Authentication (SSO) deployment protection is now fully
  disabled project-wide** (`ssoProtection.enabled: false`, confirmed via
  the Vercel API's `get_project` — not visible in `vercel project
  inspect`'s CLI output, see CLAUDE.md). This was `enabled: true,
  deploymentType: "all_except_custom_domains"` before, which gated
  Production too (no custom domain existed to exclude). The
  narrower fix — scoping it to `deploymentType: "preview"` only, which
  keeps Preview gated while opening just Production, no custom domain
  needed — was suggested but the actual API call to set it was blocked
  by this environment's own safety guardrail (flagged as a
  security-weakening change, routed back to the user to do by hand). The
  user instead disabled protection entirely via the dashboard. **Side
  effect worth knowing**: `dev`'s Preview deployments are now ALSO
  publicly reachable, not just Production — if that's not wanted,
  re-enable protection scoped to "Only Preview Deployments" in Project
  Settings -> Deployment Protection -> Vercel Authentication.
- **Google sign-in is confirmed working on Production, `dev` Preview, and
  `localhost:3000`** — Production confirmed via a real merge to `main`
  (deployment `dpl_DLzHtX8beXaC1fdst3ik7bYjwiRg`, `target: "production"`,
  `READY`, confirmed via the Vercel API) plus a direct user report that
  sign-in completes there. All three environments are now working.
- **Refresh-token encryption-at-rest (CLAUDE.md's requirement) is not yet
  implemented** — deliberately, because no refresh token exists yet
  (current scope is identity-only, no `drive.file`/offline access
  requested). Must be added as part of `feature/drive-upload`, not
  bolted on after the column already has real plaintext tokens in it.
- **Redaction still clips a tiny sliver of diagram ink when a caption's
  text bbox and the diagram have zero gap between them in the source
  layout** — found on `sample_plan_2.pdf`'s "D1-A. 1 pce." caption
  (visible as a faint ~1-2px notch cut into the top of the circle arc
  directly below the text, only visible zoomed in). Root-caused by
  checking real pixel data: the drawing's topmost ink starts at the exact
  same y-coordinate the text's bbox ends at (confirmed via `get_text`'s
  raw span data — that bbox edge is `origin.y + descender*size`, i.e. the
  font's generic descender metric, not a measurement of real glyph ink;
  "1 pc."'s lowercase "p" descender genuinely reaches close to that same
  boundary too). This is a different failure mode from the earlier
  `images=2` border bug (§5) — `images=0` doesn't help here, since the
  redaction's white FILL rectangle itself (not image blanking) is what
  overlaps the diagram. Not fixable by adjusting a parameter: erasing all
  the original English text requires covering the font-metric bbox, and
  when that bbox and the diagram genuinely touch in the source PDF with
  no buffer, some overlap is unavoidable with a bbox-based redaction
  rect. A real fix would need ink-based redaction (rasterize the region,
  diff against real glyph pixels instead of trusting the font-metric
  bbox, erase only actual text ink) — a meaningfully bigger change.
  **Explicitly deferred per user request (2026-09-19)** — logged, not
  attempted. Revisit if it turns out to affect more than this one corner
  case (captions sitting flush against a diagram edge with zero gap).
- **No automated tests exist.** CLAUDE.md explicitly calls for unit tests on
  the classify/translate tokenizer and the rotation-aware overflow check —
  the two places silent bugs were already found once. Worth doing before
  this grows much further.
- **Overflow-length measurement for diagonal (non-axis) text is an
  approximation** (`reinsert.py`'s `_available_length`) — falls back to the
  original text's own rendered width rather than a true bbox projection,
  since decomposing an axis-aligned bbox into an along-direction length needs
  font ascent/descent data not currently captured. Every diagonal span found
  so far is an untranslated numeric code, so this hasn't mattered in
  practice — but it's untested against a real translated diagonal span.
- **The wrap-box and single-line-bleed fallback tiers in `_insert_span`
  are no longer hit by any real case found so far** (see §5 point 3) —
  `_deep_shrink_fit` alone resolved every real overflow found across all
  15 pages of `sample_plan.pdf` in German, plus the specific Spanish
  cases from real broken output. They're kept as fallbacks for text tier
  1 genuinely can't rescue (e.g. a whole extra sentence's worth on a tiny
  label), not because a real case currently needs them. If one ever does:
  extract.py doesn't capture diagram/image bboxes today, and a quick
  check found sample data's diagrams are one page-covering raster image
  (not per-shape vector paths, confirmed via `page.get_drawings()`
  returning empty), so there's no simple per-diagram collision boundary
  available even if this were revisited.
- **`translation_cache.py`'s `save_many` is first-writer-wins
  (`ON CONFLICT DO NOTHING`), by explicit v1 design** (see its own
  docstring) — once a Gemini translation is cached for a
  `(source_text_lower, target_lang)` pair, every later run reuses that
  exact cached string forever, even after a prompt/glossary change that
  would have produced a better translation. This is the likely
  explanation if local testing ever shows a translation that doesn't
  reflect a recent glossary/prompt edit for a phrase NOT in the static
  glossary (a static-glossary edit itself takes effect immediately next
  run regardless of this cache — `classify_and_translate` checks the
  static `GLOSSARY` dict first, tier 4/Postgres only runs on spans still
  `unmatched` after that). No admin/review UI exists yet to hand-edit or
  clear a stale row (see the existing bullet on this below) — a manual
  `DELETE FROM glossary_terms WHERE ...` against Neon is the only way
  today. Checked directly against the real DB while investigating a
  "local dev glossary seems stale" report: only 4 German rows exist
  total (for a page-15 span unrelated to what was reported), so this
  wasn't the actual cause that time — the far more likely cause for a
  genuinely-edited `glossary.py` entry not showing up locally is the
  already-documented `vercel dev` Python build-caching gotcha (clear
  `.vercel/cache`/`api/__pycache__` and restart `npm run dev:full`).
- **No image recompression on export**, despite CLAUDE.md flagging it as "a
  cheap, separate win worth doing regardless." Output PDF is currently ~9.6%
  *larger* than input (redaction/reinsertion adds content-stream overhead
  with no offsetting image optimization).
- ~~Vercel Python multi-file import gotcha is not documented anywhere
  except this file and inline code comments~~ — **fixed**: now in
  CLAUDE.md's own "Python multi-file imports on Vercel" section, along
  with the circular-import lesson from building `api/ocr_vision.py`/
  `api/geometry.py`. Still no lint rule catching either case — still has
  to be remembered manually, just no longer undocumented.
- **Blob storage has no cleanup/lifecycle policy** beyond `translate.py`
  deleting the source upload immediately after processing (best-effort, not
  guaranteed — a mid-request crash would orphan it). Translated outputs are
  never deleted at all; they'll accumulate indefinitely. Fine for MVP/demo
  volume, not fine at any real scale.
- **No rate limiting / abuse protection** on `/api/translate` — it's an
  unauthenticated endpoint that runs a real PDF pipeline, calls the Gemini
  API, and writes to Blob storage on every call. Gemini rate limiting
  itself is in-process-only, not distributed (see §4) — a real concern
  once concurrent traffic exists, not yet.
- **No admin/review UI for a wrong Gemini-generated cache entry.** The
  schema supports fixing one directly (`UPDATE`/`DELETE` on
  `glossary_terms` by `(source_text_lower, target_lang)`), but there's no
  UI for it — a human has to go to the database directly today.
- **Gemini's 500 req/day free-tier cap isn't actively tracked** (no shared
  counter) — a quota-exceeded response is just another failure the
  existing fallback path already handles gracefully (spans stay
  `unmatched`/`needs_review`), so it degrades rather than crashes, but
  there's no visibility into how close to the daily cap usage is.
- **French/Spanish accented-character rendering is verified only for the
  specific characters the sample PDF happens to produce** (é, ñ, ¿, ¡, à,
  ç — all render fine with the existing base-14 `"helv"` font). French
  `œ`/`Œ` specifically is outside that font's WinAnsi/Latin-1 encoding and
  has NOT been exercised by real content yet — flagged, not assumed fine,
  if a real French document ever produces one.

## 8. Commands to know

```bash
# Local dev — full stack (Next.js + Python functions + Blob) — USE THIS ONE:
npm run dev:full

# Local dev — Next.js only, does NOT serve api/*.py (will break API calls):
npm run dev

# Build / lint (no test script exists):
npm run build
npm run lint

# Pull latest env vars (incl. Blob token) from Vercel after any storage change:
vercel env pull .env.local --yes

# Force vercel dev to rebuild a Python function's dependencies instead of
# silently reusing a stale build (needed after any requirements.txt
# change -- see §4's native-extension note for why this matters):
rm -rf .vercel/cache api/__pycache__

# Inspect/manage the Blob store directly:
vercel blob list --rw-token "$BLOB_READ_WRITE_TOKEN"
vercel blob del <url> --rw-token "$BLOB_READ_WRITE_TOKEN"

# Run the Python pipeline stages standalone (outside Vercel) for quick
# iteration — needs a venv with requirements.txt installed; each api/*.py's
# module-level functions (extract_document, classify_translate_document,
# reinsert_and_export, etc.) are directly importable and testable this way:
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -r requirements.txt
# then e.g.:
# PYTHONPATH=api /tmp/venv/bin/python -c "import extract; ..."

# Sync dev after a PR merge (repeat pattern used all session):
git fetch origin
git checkout dev
git merge --ff-only origin/dev
git checkout -b feature/<name>

# Prisma migrations (auto-loads prisma7.config.ts -> DIRECT_URL; despite
# the filename, no --config flag is needed, Prisma finds it on its own):
npx prisma migrate dev --name <description>   # create + apply a migration
npx prisma generate                            # regenerate app/generated/prisma
                                                # without a migration (e.g. after
                                                # a manual schema edit + npx prisma db push)
# ^ also runs automatically via package.json's "postinstall" script on
# every `npm install`, including Vercel's build -- app/generated/prisma
# is gitignored, this is what recreates it on a fresh clone. Don't remove
# the postinstall script.

# Inspect real Vercel project settings the CLI doesn't surface (e.g. SSO/
# password deployment protection) -- use the MCP tool, not `vercel project
# inspect`, which omits them:
# mcp__plugin_vercel_vercel__get_project with idOrName = the project ID

# After changing a Vercel env var's value (not just adding a new one),
# the change does NOT apply to already-running deployments -- Vercel
# snapshots env vars per-deployment at build time. Trigger a new
# deployment (empty commit, `vercel redeploy <url>`, or the dashboard's
# Redeploy button) before assuming a fix took effect.
```
