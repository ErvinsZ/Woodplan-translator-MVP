@AGENTS.md

---

Read PROJECT_STATE.md in full before doing any work in this repo, at the start
of every session — this file (CLAUDE.md) is loaded automatically, but
PROJECT_STATE.md is not, and it has the actual current architecture, decisions
made and why, and what's in progress. Don't rely on stumbling onto it.

Keep PROJECT_STATE.md up to date as we work. After completing any meaningful 
step (finishing a feature, making an architectural decision, fixing a bug), 
update the relevant section rather than waiting to be asked. Keep it concise — 
this file should always reflect current reality, not a history log.

---

# Project: Woodworking Plan Localization SaaS (MVP)

## What this is

A tool that takes an Etsy seller's woodworking/DIY build-plan PDFs (originally
in English) and produces localized versions (starting with German) while
preserving the exact technical layout: dimension callouts, part codes,
cutout lists, scale diagrams. Sold as a self-serve SaaS to other Etsy plan
sellers — a niche currently only served by expensive CAD-translation
agencies or generic layout-preserving PDF translators, neither of which
targets small creators at this price point.

Primary test file: a real Etsy woodworking plan PDF (15 pages, ~14MB,
"Flip-Top Miter Saw Workbench") — see `sample_plan.pdf` and
`pipeline_tested.py` in this repo for the actual working proof-of-concept
built against it.

## Core technical finding (already validated against real data)

The source PDFs are **hybrid**: all text (labels, dimension strings like
`3'-1 3/8"`, part codes like `D1`/`S2`, instructional sentences, legal
boilerplate) is **real vector text** extractable exactly via PyMuPDF —
zero OCR needed, zero OCR error risk. The 3D diagram renders themselves
are separate embedded raster images with **no text baked into their
pixels**. This means:

- Extraction is 100% exact (no computer-vision guessing).
- Re-insertion is a data-editing problem (delete old text object, write
  new one at same position/font/rotation), not an image-editing problem.
- The large file size (~14MB for 15 pages) comes entirely from lightly-
  compressed embedded diagram images, not from text — re-encoding those
  as optimized JPEGs is a cheap, separate win worth doing regardless.

## Pipeline architecture (extraction is the only format-specific part)

```
[Extract] -> [Classify] -> [Translate] -> [Refit/Overflow check] -> [Reinsert] -> [Export]
```

- **Extract** (format-specific): PyMuPDF (`page.get_text('dict')`) for
  vector PDFs. Falls back to OCR (Google Vision/Azure) only for
  flattened/rasterized plans — same downstream pipeline either way, just
  a different extractor feeding the same JSON schema.
- **Classify**: separate each text span into "numeric/code, never
  translate" (dimension strings, `D#`/`S#` codes, page numbers) vs.
  "translatable words." Do this with regex/rules, not an LLM call — it's
  cheap and needs to be exact.
- **Translate**: check a **glossary/translation-memory dictionary FIRST**
  (exact phrase match) before falling back to per-word tokenization.
  Only hit an LLM/MT API for genuinely novel strings not in the glossary.
  This genre (woodworking plans) is highly formulaic — most content
  across a seller's whole catalog is the same ~50-100 recurring phrases
  ("Top view", "Cutout List", "Scale 1:10", legal boilerplate, etc.),
  so the glossary should cover the large majority of content after the
  first few documents.
- **Refit**: check whether translated text fits in the original span's
  footprint. German (and other languages) routinely run longer than
  English — this WILL overflow on short labels. Shrink font down to a
  floor (~75-80% of original) before flagging for human review.
- **Reinsert**: place text at the exact original baseline origin (use
  PyMuPDF's `span['origin']`, NOT `insert_textbox` into the bbox — see
  known bug #1 below). Never touch the underlying diagram image.
- **Export**: re-save the PDF. Consider re-compressing embedded images
  here too (separate win, same step).

## Known bugs already found and fixed (do not reintroduce these)

1. **`insert_textbox` into a tight single-line bbox silently draws
   NOTHING.** Single-line spans often have bbox height ≈ font size, which
   doesn't leave room for `insert_textbox`'s default line-spacing
   calculation — it fails to fit and renders nothing at all, with no
   error. Fix: use `page.insert_text(origin, text, fontsize=...)` at the
   span's exact baseline origin point instead of flowing into a box.

2. **Text rotation/direction lives on the LINE, not the SPAN, in
   PyMuPDF's `get_text('dict')` output.** Reading `span.get('dir')`
   always returns `None` — you must read `line.get('dir')` and propagate
   it down to each span in that line. Getting this wrong causes false
   "overflow" flags on every vertical/rotated dimension label, because
   the check compares translated text width against the wrong axis
   (bbox width instead of bbox height for rotated text).

3. **Glossary lookups must not naively `.strip()` before matching if
   glossary keys retain original leading/trailing whitespace** (many
   spans do, e.g. `"Do not share the files with anyone without "` with
   a trailing space, used for natural line-wrap spacing in the original
   layout). Stripping before lookup causes silent glossary misses even
   when the key visually looks identical. Store/match consistently —
   either always strip both sides, or preserve and reapply whitespace
   explicitly around the lookup.

4. **Multi-word glossary phrases must be checked BEFORE falling back to
   per-word tokenization**, and phrase matching should be
   case-insensitive. A per-token fallback (needed for mixed spans like
   `"S2 screws, 16 pc."` where codes and words share one text object)
   will never match a multi-word entry like `"Screws on connections"`
   if it tokenizes first — check the whole stripped string against the
   glossary (case-insensitively) before splitting into tokens.

5. **Mixed text spans are common and must be tokenized, not skipped
   wholesale.** E.g. `"S2 screws, 16 pc."` is ONE PyMuPDF text object
   containing a part code, a translatable word, and a count. Naive
   span-level classification (translate whole span vs. skip whole span)
   either mistranslates the code or fails to translate the word. Tokenize
   on whitespace/punctuation, classify each token, preserve codes as-is,
   translate only real words.

6. **`page.apply_redactions()`'s default `images=2` ("blank out
   overlapping image parts") bakes a visible boundary artifact onto the
   page wherever a redaction rect merely touches a raster image's
   bounding box** — even when no diagram pixels are visibly "under" the
   text being redacted. Root-caused by toggling `apply_redactions`'s
   `images=`/`graphics=` params independently against real sample pages
   and diffing pixel values: only `images=2` produced it, `images=0` and
   `images=1` never did, and `add_redact_annot(..., cross_out=...)` made
   no difference either way. Always pass `images=0` when redacting near a
   diagram — this pipeline never intends to touch image content during
   redaction in the first place, so there's no case where the default's
   blanking behavior is actually wanted.

7. **A translated span that still overflows even at the shrink floor
   must not be rendered as a single unclipped `insert_text` line** — it
   runs straight past its own column border and paints over whatever
   sits to the right (neighboring cell text, or a diagram), and
   `needs_review` being set doesn't stop the bad render from shipping
   (no review UI exists yet — see PROJECT_STATE.md). Before falling back
   to wrapping (which risks overlapping the diagram/next line below) or
   bleeding, try shrinking well past the normal 75-80% floor (down to
   ~40% of original / a ~6pt absolute minimum) to see if the whole
   translation still fits back on the span's ORIGINAL single line —
   scanning every page of the real sample PDF found this alone resolves
   the large majority of real overflow cases, since most are short
   labels where the target language just runs a bit longer, not whole
   extra sentences.

## Tech stack decisions

- **Frontend/hosting**: Next.js on Vercel.
- **Auth**: NextAuth/Auth.js with Google OAuth. Request the narrow
  **`drive.file`** scope (not full `drive`) — only lets the app manage
  files it creates itself, avoids Google's heavier "restricted scope"
  security review, and is the honest minimum needed for "auto-save
  translated plan to the user's Drive."
- **Database**: Postgres via Supabase or Neon (both have free tiers
  suitable for MVP scale). Relational is the right default here — real
  structured relationships exist (`users -> documents -> pages ->
  text_boxes`, plus a `glossary_terms` table per seller/language).
  Use a `JSONB` column on `text_boxes` for pipeline-specific fields
  still likely to change shape during iteration (overflow_flag,
  rotation_angle, reviewed_by, etc.) rather than rigidly migrating the
  schema every time a new field is needed.
  - Refresh tokens (for Drive auto-save) must be encrypted at rest.
  - Vercel serverless functions are stateless between invocations — the
    DB is what makes "connect once, auto-save forever" work at all.
- **Editor (review/quick-fix UI)**: Fabric.js or Konva.js canvas.
  Background layer = original diagram image, overlaid editable objects
  = text boxes from the pipeline's JSON. Keep v1 editing scope to: text
  content, font size, position nudge. Do NOT build diagram/arrow editing
  — if a label truly can't fit even after refit, flag it for the plan's
  original creator, don't try to solve it in-editor.
- **Translation engine**: do NOT try to run a local neural MT model
  (e.g. Argos Translate) inside a Vercel serverless function — model
  loading (50-200MB) is a poor fit for serverless cold starts/bundle
  limits. Options in order of recommendation for MVP:
  1. Glossary/dictionary lookup (free, run anywhere, covers most content
     in this genre — build this first).
  2. LLM API call (Anthropic/OpenAI/etc.) as fallback for text not in
     the glossary — simple outbound HTTP call, fits serverless fine,
     real MVP-stage cost is fractions of a cent per document.
  3. (Later optimization, not needed for MVP) self-hosted open-source MT
     (e.g. LibreTranslate) on a small always-on box (Railway/Fly.io/
     Render) if volume ever justifies flat-cost hosting over per-request
     API billing.

## Git workflow

- `main` — always deployable (protected, no direct pushes).
- `dev` — integration branch, merge finished features here first.
- `feature/*` — one branch per feature, off `dev`, merged back via PR
  (squash-merge, one commit per feature).
- Suggested feature branch order (riskiest/most-uncertain first):
  1. `feature/pdf-extraction`
  2. `feature/classify-translate`
  3. `feature/reinsertion-export`
  4. `feature/auth`
  5. `feature/db-schema`
  6. `feature/drive-upload`
  7. `feature/editor-mvp`
- Use Vercel's automatic PR preview deployments as part of review —
  actually open the preview and look at a rendered translated PDF/editor
  before merging, don't just review the diff.
- Neon supports per-branch database branching tied to git branches —
  use it so a schema-changing feature branch doesn't corrupt `dev`'s
  data while testing in parallel.
- Write a handful of real unit tests for the classify/translate
  tokenizer and the rotation-aware overflow check specifically — these
  are the two places we already found silent, non-obvious bugs.
  Everything visual (editor, rendered PDF) can be checked by eye via
  Vercel previews rather than automated for the MVP.
- Ship `dev` -> `main` in small working increments (e.g. "upload PDF ->
  get translated PDF back" is a demoable, shippable slice on its own,
  well before auth/Drive/editor exist) rather than waiting for every
  feature to land before the first merge to `main`.

## One-week build plan (assume ~3-4 focused hours/day, not all-nighters)

- **Day 1-2**: Pipeline core — extraction, classify, glossary translate,
  output the JSON schema. Get this schema shape right early; everything
  else depends on it.
- **Day 3-4**: Editor — canvas + overflow highlighting + text/font/
  position editing, single-page only for MVP.
- **Day 5**: Export — canvas state back to a clean, print-quality PDF.
  Start this early in the week, it tends to eat more time than expected.
- **Day 6**: Glue pipeline -> editor -> export end-to-end on one real
  sample plan. Expect friction here (JSON schema will be missing a
  field) — that's normal, budget time for it.
- **Day 7**: Buffer + demo polish.
- Explicitly cut from MVP scope: multi-language support (build German
  end-to-end first, generalize later), auth/payments, font-matching to
  original typeface, automated review-scoring beyond the overflow check.

## Business context (for judgment calls, not implementation)

- Target customer: small Etsy plan sellers, not engineering firms —
  underserved niche between "expensive CAD translation agency" and
  "generic PDF translator not built for dimensioned technical drawings."
- Stakes are real: a wrong digit in a dimension is not a cosmetic bug,
  it can waste a customer's materials or create a safety issue. Numbers/
  codes must NEVER pass through translation/generation — always
  extracted and reinserted byte-for-byte identical.
- Human review is part of the product, not a failure state: ship the
  glossary-covered majority automatically, flag genuine overflow/
  ambiguous cases for a quick human pass. Do not market this as fully
  unattended.


---

## Environment & tooling constraints (learned the hard way — do not violate)

### Version discipline
- NEVER install packages on a `-rc`, `-beta`, `-alpha`, `-next`, or `-experimental` tag.
  Always pin the latest STABLE release. If `npm install` offers a pre-release
  (e.g. it once offered `prisma@8.0.0-rc.15`), decline and pin the stable major
  explicitly instead.
- `npm config set save-exact true` is set for this project — installs write exact
  versions, not caret ranges. Keep it that way.
- Confirmed-stable versions in use: Prisma 7.x, @prisma/client 7.x, next-auth 4.x.
  Do not upgrade these to a new major without explicit instruction.

### Prisma 7 specifics (v7 differs significantly from older Prisma in training data)
- **Config is file-first via `prisma.config.ts`** (NOT the old `schema.prisma`
  datasource url/directUrl pattern). `directUrl` was REMOVED from `schema.prisma`
  in v7 — do not add it there.
- **CLI commands** (`db push`, `migrate`) read `datasource.url` from
  `prisma.config.ts`, which is pointed at **`DIRECT_URL`** (the direct,
  non-pooled connection). This is required — running migrations/DDL
  through a pooled transaction-mode connection HANGS silently because
  PgBouncer (or equivalent) in transaction mode can't handle the
  session-level locks Prisma needs.
- **Runtime Prisma Client uses the POOLED `DATABASE_URL`** — correct for
  many concurrent serverless connections. This project runs on **Neon**
  (as of the dynamic-translation feature — see PROJECT_STATE.md for why
  Neon, signed up directly rather than via Vercel Marketplace), whose
  pooling is **hostname-based** (`-pooler` suffix on the host, same port)
  — NOT the port-based split (6543 pooled / 5432 direct) that Supabase
  uses. Don't assume the port-based pattern if this project ever
  reconnects to Supabase or another provider instead; check that
  provider's own docs for how it distinguishes pooled vs. direct.
- **Prisma 7 REQUIRES a driver adapter to instantiate the client.**
  `new PrismaClient()` alone DOES NOT WORK in v7. Correct pattern:
```typescript
  import { PrismaClient } from './generated/prisma/client';
  import { PrismaPg } from '@prisma/adapter-pg';

  const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL });
  const prisma = new PrismaClient({ adapter });
```
  (`@prisma/adapter-pg` is installed for this.)

### Env files
- `.env.local` is the SINGLE source of truth for secrets (gitignored). There is
  no `.env` file — it was deleted to avoid Prisma silently loading placeholder
  values from it.
- `prisma.config.ts` explicitly loads `.env.local` via
  `config({ path: ".env.local" })` at the top, because the default `dotenv/config`
  import only reads `.env`, not `.env.local`.
- Env vars: `DATABASE_URL` (pooled), `DIRECT_URL` (direct) — see the Prisma
  7 section above for how pooled/direct are distinguished on this
  project's actual provider (Neon, hostname-based) — plus NextAuth +
  Google OAuth + `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`. All must also be
  set in Vercel's project environment variables for deploys.

### Package manager
- This project uses **npm**, not pnpm. Use npm commands throughout.

### Python dependencies on Vercel (compiled extensions break at runtime)
- NEVER add a Python package with a compiled/native extension to
  `api/*.py`'s `requirements.txt` without testing it via a REAL
  `vercel dev` request first — a clean install in a plain local venv
  proves nothing about whether it'll actually run. `google-genai` (pulls
  in `pydantic`/`pydantic-core`, a compiled Rust extension) and
  `psycopg[binary]` (compiled `libpq`/`psycopg_c`) both installed and
  imported fine in a local venv, then crashed EVERY request on real
  `vercel dev` with `NO_RESPONSE_FROM_FUNCTION`
  (`ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'`,
  then `ImportError: no pq wrapper available`) — a platform mismatch
  between the built wheel and Vercel's actual Python function runtime.
  This is a known, recurring, *unresolved* issue for these specific
  packages on Vercel (see Vercel's own community forum and pydantic's
  GitHub issues), not specific to this repo, and it can also affect real
  deployments, not just local `vercel dev` emulation.
- Fixed by using pure-Python alternatives instead: raw REST via `urllib`
  for the Gemini API (`api/gemini_translate.py`, same pattern as
  `api/blob.py`'s hand-rolled Vercel Blob client — no official SDK
  needed) and `pg8000` (pure Python, DB-API 2.0) for Postgres instead of
  `psycopg` (`api/db.py`).
- Before trusting a new Python dependency, check whether it ships a
  compiled extension, and confirm it with a real request against
  `npm run dev:full` — clear `.vercel/cache` and `api/__pycache__` first,
  or `vercel dev` will silently keep serving the previous build and hide
  the problem.

---

## Design Guidelines

Avoid generic "AI-generated" website patterns. Be deliberate and specific to this product, not templated.

### Don't default to:
- Centered hero with gradient blob background + oversized bold headline + two pill buttons
- Purple-to-blue or blue-to-teal gradients (the AI-website cliché)
- Symmetric 3-column feature grids with icon-title-description repeated identically
- Glassmorphism/frosted-blur cards as the go-to "modern" look
- Inter/system-ui with no real type scale or hierarchy
- Generic outline icon sets (checkmarks in circles, lightning bolts, rockets) as filler
- Cartoon stock-illustration people at laptops
- "Fade up on scroll" applied uniformly to every element, or hover-scale on every card
- SaaS filler copy: "Supercharge your workflow," "Unlock your potential," "Everything you need in one place"

### Instead:
- Pick one strong, specific visual idea (an unusual color, a layout quirk, a distinct type pairing) rather than stacking trendy effects
- Choose colors, type, and layout based on what this product/brand actually is
- Vary section rhythm and spacing — not every section needs identical padding/centering
- Copy should be concrete and specific to the product, not generic benefit-speak
- Minimal, asymmetric, dense, or unconventional layouts are fine if they fit the content

### Next.js specifics:
- Don't reach for shadcn/ui defaults unstyled — theme them intentionally (colors, radii, spacing) rather than shipping default Tailwind/shadcn look
- Avoid the default `next/font` Inter/Geist setup without considering a more distinctive pairing
- Check `tailwind.config` for an existing design system/tokens before inventing new ad hoc colors per component