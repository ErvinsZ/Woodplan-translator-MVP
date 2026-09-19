# Project State

Last updated: 2026-09-19, after a second pass fixing `reinsert.py`
rendering bugs (redaction border artifact over diagrams; hardcoded font;
overflow bleed/wrap-into-diagram, resolved via a deep-shrink-first
fallback) plus one `glossary.py` content rebalance, verified against real
broken-output repros the user generated locally — not yet on a merged
branch, see §6. Previous entries: after merging dynamic Gemini translation
+ Spanish/French support (PR #4).
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
  writes (see `pipeline` sub-object below).
- `api/classify_translate.py` — consumes extract.py's JSON, classifies each
  text box (`code` / `glossary_match` / `mixed` / `llm_match` / `unmatched`)
  and fills in `pipeline.translated_text` + `needs_review`. Tiers, in order:
  (1) regex code/number match, (2) whole-span static-glossary match,
  (3) per-token mixed-span match (codes + words sharing one span), (4) a
  whole-document pass (`_apply_llm_fallback`, runs once per document, not
  per-page — dedup needs cross-page visibility): every span still
  `unmatched` after 1-3 is deduped by stripped+lowercased text, checked
  against the Postgres dynamic cache (`api/translation_cache.py`), and
  whatever still misses goes to Gemini (`api/gemini_translate.py`) with
  full-sentence context (never per-word — that's what breaks homographs
  like "board" the lumber vs. "board" a ship). New Gemini translations are
  written back to the cache. Tiers 1-3 have no LLM call at all — glossary
  lookup only, case-insensitive, whitespace-safe. Imports `api/glossary.py`,
  `api/translation_cache.py`, `api/gemini_translate.py`.
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
        "classification": "code"|"glossary_match"|"mixed"|"llm_match"|"unmatched"|null,
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
  access — see §4 for why) for PDFs in/out. Postgres (Neon, provisioned
  directly at neon.tech — see §4 for why not via Vercel Marketplace) for
  the dynamic translation-memory cache (`glossary_terms` table via
  Prisma's `schema.prisma`/migrations — no other tables yet, no
  users/documents/pages schema; that's still `feature/db-schema`'s job).
  `DATABASE_URL` (pooled) / `DIRECT_URL` (direct) are real, in `.env.local`
  and all three Vercel environments.
- **Package manager**: npm, not pnpm. `save-exact=true` via `.npmrc`
  (project-level file — global npm config does NOT have this set, don't
  rely on it).
- **Python deps**: `requirements.txt` at repo root, pinned exact versions
  (`pymupdf==1.28.2`, `certifi==2026.7.22`, `pg8000==1.31.5`). Deliberately
  NO compiled/native-extension packages — see §4's native-extension
  decision for why. No venv checked in; Vercel's
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
  Postgres provisioned and connected (`glossary_terms` table only — see §4
  for why global/unscoped and why signed up directly at neon.tech).
  `GEMINI_API_KEY` (Google AI Studio free tier) live in `.env.local` and
  all three Vercel environments.
- Four PRs merged to `dev`: #1 (extraction), #2 (reinsertion/export +
  glossary expansion), #3 (frontend), #4 (dynamic Gemini translation +
  Spanish/French + the native-extension fix in §4).
- Fixed several visible rendering bugs in `reinsert.py`/`glossary.py`,
  all root-caused against real output from `reference/sample_plan.pdf`,
  `reference/sample_plan_2.pdf`, and broken-output repros the user
  generated locally (`reference/sample_plan_broken_{de,es}.pdf`,
  `reference/sample_plan_2_broken.pdf` — not yet on a merged branch, see
  §6):
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

## 6. What's in progress

All `reinsert.py`/`glossary.py` fixes from §5 are complete and verified
against real output, on **`feature/reinsert-render-fixes`** (branched off
`dev`, not yet committed/pushed/PR'd — the branch exists but nothing has
been committed to it yet; carries the same working-tree changes `dev` had
uncommitted before the branch was created). Before committing: this is a
bug fix to already-merged code rather than new feature work, so a direct
squash-ish single commit is probably right per the usual PR convention,
still opened as a normal PR into `dev` per CLAUDE.md's workflow.
`reference/sample_plan_2.pdf` is also untracked (a second real test
fixture, Calibri/Verdana-based, added to reproduce the border-artifact
bug) along with three more real broken-output repros the user generated
locally while testing this fix in progress —
`reference/sample_plan_2_broken.pdf`,
`reference/sample_plan_broken_de.pdf`,
`reference/sample_plan_broken_es.pdf` — all four worth committing
alongside if kept as permanent reference/regression fixtures (they're
genuinely useful: every real bug found and fixed this session was
root-caused directly against one of them, not guessed from screenshots
alone).

Otherwise nothing else mid-edit. The natural next slice (per CLAUDE.md's
suggested branch order, adjusted for what's already built) is one of:
- `feature/auth` — NextAuth/Auth.js + Google OAuth, `drive.file` scope only.
  `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`NEXTAUTH_SECRET`/`NEXTAUTH_URL`
  are already in `.env.local` but are placeholder/local-only — not yet
  provisioned in Vercel project settings, not yet wired into any code.
- Extending `feature/db-schema` beyond `glossary_terms` — the full
  `users -> documents -> pages -> text_boxes` schema CLAUDE.md originally
  described is still not started; only the standalone translation-cache
  table exists so far. Depends on `feature/auth` existing first for
  `users` to mean anything.
- `feature/editor-mvp` — explicitly deprioritized ("skip the editor for
  now, as it's an extra feature") in favor of the no-login frontend.
  Fabric.js/Konva.js canvas, not started at all.
- `feature/drive-upload` — not started, depends on auth existing first.
  Per this session's Q&A: it would attach to the already-produced PDF
  bytes from `reinsert_and_export` in `translate.py`, as an additional
  output destination alongside/instead of the Blob upload — it wouldn't
  touch extraction/classification/translation at all.

## 7. Known issues / TODOs

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
```
