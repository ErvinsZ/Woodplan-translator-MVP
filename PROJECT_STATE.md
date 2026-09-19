# Project State

Last updated: 2026-09-19, end of session that built the pipeline + frontend mockup.
See `CLAUDE.md` for the original plan/spec; this file tracks actual current state,
which has diverged from and extended that plan in places noted below.

## 1. Project goal

A SaaS tool that takes an Etsy seller's woodworking/DIY build-plan PDFs (English)
and produces localized versions (German first) while preserving exact technical
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
  writes (see `pipeline` sub-object below).
- `api/classify_translate.py` — consumes extract.py's JSON, classifies each
  text box (`code` / `glossary_match` / `mixed` / `unmatched`) and fills in
  `pipeline.translated_text` + `needs_review`. No LLM call — glossary lookup
  only, case-insensitive, whitespace-safe. Imports `api/glossary.py`.
- `api/glossary.py` — pure data. `GLOSSARY[english_text][lang_code] = translation`.
  114 German entries, built by sweeping all 15 pages of the sample PDF. Adding
  Spanish later is a data-only change (add `"es"` keys), no code change.
- `api/reinsert.py` — refit (font-shrink toward 75% floor), redact original
  text, reinsert translated/unchanged text at exact baseline origin. Handles
  arbitrary rotation angles via `morph=` matrix (not just axis-aligned).
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
- `app/page.tsx` — the entire UI. Client component (`"use client"`). Upload
  (drag-drop or click) -> `@vercel/blob/client`'s `upload()` sends the PDF
  straight to Blob storage -> POST `{blob_url, filename}` to
  `/api/translate?lang=de` -> renders the returned `download_url` as a link.
  No login, no persistence, no history — single-shot tool.
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
      "pipeline": {                 # empty at extraction, filled in later
        "classification": "code"|"glossary_match"|"mixed"|"unmatched"|null,
        "target_lang": str|null, "translated_text": str|null,
        "needs_review": bool,
        "overflow": bool|null, "used_font_size": float|null, "available_len": float|null
      }
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
  access — see §4 for why). No database wired up yet (Prisma installed,
  schema is still the untouched scaffold in `prisma/schema.prisma`).
- **Package manager**: npm, not pnpm. `save-exact=true` via `.npmrc`
  (project-level file — global npm config does NOT have this set, don't
  rely on it).
- **Python deps**: `requirements.txt` at repo root, pinned exact versions
  (`pymupdf==1.28.2`, `certifi==2026.7.22`, `pg8000==1.31.5`). No compiled/
  native-extension packages (see §6's note on why — `psycopg`/
  `google-genai` both broke real `vercel dev` despite working in a local
  venv). No venv checked in; Vercel's
  `uv`-based builder provisions one per function at build time.
- **Design system**: warm "drafting paper" palette (see globals.css tokens),
  Space Grotesk (display) + IBM Plex Mono (labels/mono), explicitly avoiding
  generic AI-SaaS visual patterns — see CLAUDE.md's "Design Guidelines"
  section for the full negative/positive list.
- **Git workflow**: `main` (protected) <- `dev` (integration) <- `feature/*`
  (one per feature, squash-merged via PR). Three PRs merged so far, in order:
  `feature/pdf-extraction` (#1), `feature/reinsertion-export` (#2),
  `feature/frontend-mockup` (#3). Combined commits are fine per-branch (user
  preference: don't split a feature's work across multiple commits/PRs
  unnecessarily).
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
- **No LLM translation fallback yet**, by explicit scope decision — anything
  the glossary doesn't cover is left in English with `needs_review: true`
  rather than machine-translated. This is intentional per CLAUDE.md's
  translation-engine priority order (glossary first, LLM fallback later,
  self-hosted MT only if volume justifies it), not an oversight.
- **`package.json`'s `dev` script stays `next dev`, not `vercel dev`.**
  `vercel dev` internally shells out to `npm run dev` to start the framework
  dev server — pointing `dev` at `vercel dev` itself causes infinite-
  recursion errors. The full-stack-including-Python-functions dev command is
  `npm run dev:full` (= `vercel dev`). **`npm run dev` alone does NOT serve
  `api/*.py` at all** — hitting `/api/translate` under plain `next dev`
  returns Next.js's HTML 404 page, which breaks frontend `res.json()` calls
  with `"Unexpected token '<'"`. This is the single most likely thing to trip
  up a fresh session — see §7.

## 5. What's done

- Full pipeline: extract -> classify/translate -> refit/reinsert/export,
  tested against all 15 pages / 533 spans of the real sample PDF, zero
  crashes, visually verified (including diagonal rotation and multi-line
  sentence reconstruction) via rendered PNG diffs.
- German glossary: 114 entries, covers all but 4 deliberately-flagged spans
  of the sample PDF (down from 134 flagged with the original 20-entry
  glossary).
- Frontend: upload/translate/download flow, no login, verified end-to-end
  through a real browser against real `vercel dev` + real Vercel Blob (not
  mocked). `npm run build` and `npm run lint` both pass clean.
- Vercel Blob store provisioned and connected (`woodplan-translator-blob`,
  public, static `BLOB_READ_WRITE_TOKEN` + OIDC both available).
- Three PRs merged to `dev`: #1 (extraction), #2 (reinsertion/export +
  glossary expansion), #3 (frontend).

## 6. What's in progress

**Dynamic Gemini-backed translation + Spanish/French support** — built and
verified this session, committed to `feature/dynamic-translation` (not
merged/pushed yet). Plan lives at
`~/.claude/plans/wiggly-imagining-stream.md`. Adds a 4th classify/translate
tier: spans the static glossary misses now check a Postgres-backed dynamic
cache (`glossary_terms` table) first, then fall back to a Gemini API call
with full-sentence context (to resolve homographs like "board" the lumber
vs. "board" a ship — a naive per-word fallback would get these wrong).
Successful Gemini translations are saved back to the cache, so repeat
occurrences (within a document or across future documents/sellers) hit the
cache instead of calling the API again.

- **New Postgres database provisioned**: Neon, created directly at
  neon.tech (NOT via `vercel integration add neon`) — deliberately, because
  Vercel's marketplace-managed billing wrapper reportedly has no hard spend
  cap once free-tier usage is exceeded, whereas Neon's own direct free tier
  is hard-capped (100 CU-hours/month, 0.5GB) with no card on file. `DATABASE_URL`
  (pooled, hostname has `-pooler`) and `DIRECT_URL` (direct) are real now,
  in both `.env.local` and all three Vercel project environments — no
  longer placeholders. Note: Neon's pooling is hostname-based
  (`-pooler` suffix), not port-based like Supabase's 6543/5432 split this
  file previously described — same pooled-for-runtime/direct-for-migrations
  concept, different URL shape.
- **New Prisma model**: `GlossaryTerm` (`@@map("glossary_terms")`) in
  `prisma/schema.prisma`, migrated via `prisma migrate dev` against
  `DIRECT_URL` (migration `20260919102204_add_glossary_terms`). Keyed on
  `(source_text_lower, target_lang)` unique constraint — stripped +
  lowercased text, deliberately different from the static `GLOSSARY`
  dict's raw-whitespace-preserving keys (see `api/translation_cache.py`'s
  docstring for why). **No Prisma Python client exists** — runtime
  reads/writes from `api/*.py` go through `pg8000` directly (`api/db.py`),
  not Prisma; Prisma only owns schema/migrations here.
- **New files**: `api/db.py` (pg8000 connection helper — see the native-
  extension note below for why pg8000, not psycopg), `api/translation_cache.py`
  (cache lookup/save, fails soft on any DB error), `api/gemini_translate.py`
  (Gemini REST client — see below for why raw REST, not the `google-genai`
  SDK — with a system prompt built from the static glossary for
  consistency/homograph guidance, chunking + in-process pacing to respect
  free-tier rate limits, retry with backoff on 429/5xx, never raises past
  a single chunk's failure).
- **IMPORTANT, non-obvious, cost real debugging time this session: don't
  add Python dependencies with compiled/native extensions to `api/*.py`
  without testing them through real `vercel dev` first**, not just a
  local venv. `google-genai` (pulls in `pydantic`/`pydantic-core`, a
  compiled Rust extension) and `psycopg[binary]` (compiled `libpq`/
  `psycopg_c`) both imported and worked fine in a plain local venv, then
  crashed **every single request** on real `vercel dev` with
  `NO_RESPONSE_FROM_FUNCTION` / `ModuleNotFoundError: No module named
  'pydantic_core._pydantic_core'` and, after removing that, `ImportError:
  no pq wrapper available` — a platform mismatch between whatever
  built the wheel and Vercel's actual Python function runtime. This is a
  known, recurring, *unresolved* problem for these specific packages on
  Vercel (see Vercel community forum / pydantic GitHub issues), not
  something specific to this repo's setup, and per those threads it can
  also bite real deployments, not just local `vercel dev` emulation.
  Fixed by dropping both for pure-Python alternatives: `gemini_translate.py`
  now calls the Gemini REST API directly via `urllib` (same pattern as
  `api/blob.py`'s hand-rolled Vercel Blob client — verify the REST
  contract with a real `curl` call first, same as blob.py's docstring
  describes doing), and `api/db.py` uses `pg8000` (pure Python, DB-API
  2.0, parses `DATABASE_URL` by hand since it has no built-in DSN
  parsing, needs an explicit `certifi`-based SSL context for the same
  "ambient CA bundle unreliable" reason as blob.py — see its docstring).
  **`pg8000.dbapi.Cursor` has no context-manager protocol** (unlike
  psycopg's) — don't write `with conn.cursor() as cur:`, just
  `cur = conn.cursor()`. If a future session adds another Python
  dependency, treat "does it ship a compiled extension" as a real risk
  factor and confirm via a real `vercel dev` request (clear
  `.vercel/cache` + `api/__pycache__` first, or it'll silently keep
  serving the previous build) before assuming a clean local venv install
  means it'll work.
- **Changed**: `api/classify_translate.py` gets `_apply_llm_fallback`,
  run once per document (not per-page — dedup needs cross-page
  visibility) after the existing 3 tiers; `api/translate.py`'s
  `DEFAULT_TARGET_LANG` comment updated (still `"de"` as default, but
  `es`/`fr` are now real, just with less static glossary coverage
  initially); `app/page.tsx` gets a DE/ES/FR language toggle (was
  hardcoded to `?lang=de`).
- **Model note**: `gemini-2.5-flash-lite` (the model originally planned,
  for its free-tier limits) returned `404 NOT_FOUND` on `generate_content`
  for this project's API key — "no longer available to new users," despite
  still appearing in the `/models` listing. Don't reintroduce it. Live
  model list confirmed the actual available lite model + exact ID via
  `GET /v1beta/models` (don't trust training-data model names or search
  results for this — Google changes free-tier model availability without
  much notice): **`gemini-3.1-flash-lite`**, confirmed against this
  account's real AI Studio dashboard, 2026-09-19: 15 req/min, 500 req/day,
  250k tokens/min. That's what `gemini_translate.py`'s `MODEL` constant
  and throttling constants (`_MIN_SECONDS_BETWEEN_CALLS`,
  `_MAX_TOKENS_PER_CHUNK`) are tuned for now.
- **Verified against `reference/sample_plan.pdf`, all 3 languages, with
  real Gemini calls** (not mocked): German unchanged from the
  pre-existing baseline (348 glossary_match, 165 code, 16 mixed — the 4
  previously-`unmatched` spans now correctly `llm_match` via Gemini,
  coherent translation). Spanish and French (zero static glossary
  coverage) both went from 0 static coverage to 368/533 spans correctly
  `llm_match` in a single Gemini call each. Postgres cache verified two
  ways: direct save/lookup against the real Neon DB, and a full second
  pipeline run with `gemini_translate.translate_batch` monkey-patched to
  raise if called at all — it wasn't, confirming cache hits actually skip
  the API rather than just returning the same answer coincidentally.
  Also verified with a REAL end-to-end request through real `vercel dev`
  (not just direct Python calls in a venv) — uploaded the sample PDF to
  Blob, POSTed to `/api/translate?lang=de`, got a 200 with a real
  `download_url`, downloaded and rendered it. This is what caught the
  native-extension issue above; direct-venv testing alone did not.
- **Bug found and fixed via this testing**: `reinsert.py`'s `refit_span`
  only updated `used_font_size` inside the "found a fit" branch of its
  shrink loop, so a translation that never fit even at the shrink floor
  silently rendered at the full, un-shrunk original size — worse than
  the floor size, and the opposite of CLAUDE.md's "shrink to a floor
  before flagging" spec. Pre-existing, but invisible until Gemini-fallback
  French/Spanish text (longer than the length-conscious hand-curated
  German glossary) visibly ran a page-1 heading off the right edge in a
  rendered PNG check. Fixed with a `while/else`: render at the floor size
  when the loop never finds a fit. Re-verified by re-rendering — heading
  now fits; `needs_review`/`overflow` flags still correctly fire. See
  `reinsert.py`'s own docstring for the full note.
- **Real infra provisioned this session** (previously placeholders):
  Neon Postgres (created directly at neon.tech, NOT via
  `vercel integration add neon` — that marketplace billing path reportedly
  has no hard spend cap once free tier is exceeded, whereas Neon's own
  direct free tier is hard-capped with no card on file). `DATABASE_URL`
  (pooled, `-pooler` hostname) / `DIRECT_URL` (direct) are real now, in
  `.env.local` and all three Vercel environments. `GEMINI_API_KEY` (Google
  AI Studio, free tier) likewise real in both places.

The natural next slice after this lands (per CLAUDE.md's suggested branch
order, adjusted for what's already built) is one of:
- `feature/auth` — NextAuth/Auth.js + Google OAuth, `drive.file` scope only.
  `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`NEXTAUTH_SECRET`/`NEXTAUTH_URL`
  are already in `.env.local` but are placeholder/local-only — not yet
  provisioned in Vercel project settings, not yet wired into any code.
- `feature/db-schema` — Postgres via Neon or Supabase. `DATABASE_URL` /
  `DIRECT_URL` are in `.env.local` as placeholders only; `prisma/schema.prisma`
  is still the default untouched scaffold (no models). CLAUDE.md has detailed
  Prisma 7 setup notes (driver adapter required, config via
  `prisma.config.ts` not `schema.prisma`, migrations need `DIRECT_URL` not
  the pooled `DATABASE_URL`) — read those before touching Prisma.
- `feature/editor-mvp` — explicitly deprioritized by the user this session
  ("skip the editor for now, as it's an extra feature") in favor of the
  no-login frontend. Fabric.js/Konva.js canvas, not started at all.
- `feature/drive-upload` — not started, depends on auth existing first.

## 7. Known issues / TODOs

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
- **No image recompression on export**, despite CLAUDE.md flagging it as "a
  cheap, separate win worth doing regardless." Output PDF is currently ~9.6%
  *larger* than input (redaction/reinsertion adds content-stream overhead
  with no offsetting image optimization).
- **Vercel Python multi-file import gotcha is not documented anywhere except
  this file and inline code comments.** If a fresh session adds a new
  `api/*.py` file that imports a sibling module, it WILL silently work in any
  local hand-rolled test harness (because you control `sys.path` yourself)
  and WILL silently fail on real `vercel dev`/production with
  `ModuleNotFoundError` unless the `sys.path.insert(...)` line from §2 is
  copied into the new file too. There is no lint rule or test catching this
  — must be remembered manually.
- **Blob storage has no cleanup/lifecycle policy** beyond `translate.py`
  deleting the source upload immediately after processing (best-effort, not
  guaranteed — a mid-request crash would orphan it). Translated outputs are
  never deleted at all; they'll accumulate indefinitely. Fine for MVP/demo
  volume, not fine at any real scale.
- **Single language (German) only** — by design for now, not a bug, but the
  UI hardcodes `?lang=de` with no language selector.
- **No rate limiting / abuse protection** on `/api/translate` — it's an
  unauthenticated endpoint that runs a real PDF pipeline and writes to Blob
  storage on every call.

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
```
