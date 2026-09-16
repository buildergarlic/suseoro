# SuSeoRo workspace overhaul Implementation Plan

> **For agentic workers:** Use scoped implementation and independent review with superpowers:subagent-driven-development; preserve shared file ownership.

**Goal:** Deliver a readable professional desktop workflow with reliable file imports and ISBN source links.
**Architecture:** Keep existing local backend/store contracts, modernize simple React UI, repair parsing boundaries and provider error handling without schema migration.
**Tech Stack:** React19/TypeScript/CSS, Python/FastAPI/SQLite, Calamine/openpyxl.
**Spec:** docs/superpowers/specs/2026-09-16-readable-workspace.md

## Global Constraints
- Source only in this isolated worktree; original user data untouched.
- Default16px, captions >=13px, text settings16/18/20; compact mode affects spacing.
- Maintain rows/headers/mapping and existing lookup response contracts.
- No invented ISBN details, no secret logging, no Aladin API dependency.
- Preserve autoupdate, help, support, pricing/rounding, backups and export rules.

## Task 1: Import fidelity
Owner: parsing agent. Files: simple/documents.py and ingestion parsers, matching backend tests.
- [x] Add failing fixtures for leading blank offsets, split merged headers, decorated Korean headers, mixed-sheet remapping, ISBN10/13 priority, headerless first-row retention and multilineCSV.
- [x] Repair source parsing, retain raw provenance and warnings. Keep existing output shape.
- [x] Run affected regression suites and submit diff for independent review.

## Task 2: ISBN provider correctness
Owner: API agent. Files: simple/bibliography.py and provider tests.
- [x] Reproduce real NL HTTP200 RESULT=ERROR ERR_CODE=010 response.
- [x] Handle authentication/errors explicitly; produce validated ISBN search source instead of NL homepage.
- [x] Run provider regression suite, validate official public links without key assumptions.

## Task 3: Readable task workspace
Owner: root markup/components, CSS agent stylesheet only.
- [x] Add tests for font preference persistence, safe canonical ISBN links, review-only import filtering and keyboard search.
- [x] Rebuild layout around title/commands, compact budget status, searchable table and edit drawer.
- [x] Add readable font/density options, source links in rows/editor, direct API setup guidance in lookup.
- [x] Improve import phases, review counts and source/warning display without losing original row indices.

## Task 4: Integration and delivery
- [x] Run backend regressions, frontend tests/typecheck/lint/build.
- [x] Browser inspect with synthetic data at desktop, narrow and large text; verify import→review→save→lookup/link→export.
- [x] Independent review and fix actual findings, update user docs and save screenshots/report to task outputs.
- [x] Provide reviewable local build; do not publish or modify installed app automatically.


## Final verification
- Frontend: 157 tests pass; lint/typecheck/production build pass.
- Simple backend: 237 tests pass, plus 71 shared parser/safety regressions.
- Actual user XLSX: 10/10 books, 163100 KRW, original SHA256 unchanged; GUI import and XLSX export verified.
- Browser: readable 16/20px, 1440/1024/640px widths, no document horizontal overflow, 300px table viewport at 20px.
- Native port change: display size and row density restored from SQLite, localStorage used only as cache.
- Axe A/AA checks: main/list, book drawer, ISBN modal report zero violations.
- Local Windows installer/portable build only; no live data mutation or remote publication.
