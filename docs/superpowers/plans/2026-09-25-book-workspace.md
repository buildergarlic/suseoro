# Book Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move fixed controls to a left menu, make the book table fill the working area, and let users inspect saved holdings and recommended books.

**Architecture:** Keep `SimpleLibraryApp` as the screen coordinator. Add a paginated read-only holdings endpoint, and separate registered-data view components. Rework the existing CSS layout without changing purchase-list persistence.

**Tech Stack:** React 19, TypeScript, Vite, FastAPI, SQLite, Vitest, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-25-book-workspace-design.md`

## Global Constraints

- The latest personal app source is this `codex/suseoro-ux-imports` worktree.
- The approved layout is option A from the user's 2026-09-25 choice.
- Existing text sizes are 16–24px and existing saved display settings remain valid.
- Holdings may contain 100,000 rows; the client must not load them all at once.
- Historical recommendation files have no exact committed-file relationship; group saved books by `source`.

## Review Focus

- An empty holdings table shows an honest empty state, with an import action.
- A search on a later holdings page resets to page one.
- A failed holdings request keeps a retry path visible.
- Changing purchase lists updates the recommendation view and avoids stale results.
- Narrow windows keep primary actions reachable and the table usable.

---

### Task 1: Holdings read API

**Files:** `backend/src/suseoro/simple/store.py`, `backend/src/suseoro/simple/app.py`, `backend/tests/test_simple_api.py`.

**Interfaces:** `GET /api/library/holdings?query=&page=&page_size=` returns `{items: BookFields[], total: number, page: number, page_size: number}`.

- [ ] Write failing API tests for search, pagination, empty data, and invalid paging values.
- [ ] Run the focused pytest tests and confirm the missing route or behavior fails.
- [ ] Implement bounded server-side search and page retrieval without mutating holdings.
- [ ] Re-run focused tests and the backend suite.

### Task 2: Registered-data views

**Files:** `frontend/src/simple/RegisteredLibraryViews.tsx`, `frontend/src/simple/registered-library.css`, `frontend/src/simple/api.ts`, `frontend/src/simple/types.ts`, frontend tests.

**Interfaces:** `HoldingsLibraryView({api,onImport,refreshKey})`, `RecommendationsLibraryView({books,listName})`; `api.holdings({query,page,page_size})`.

- [ ] Write failing frontend tests for view navigation, holdings search/page/retry, and recommendation grouping.
- [ ] Run focused Vitest and confirm the absent view or behavior fails.
- [ ] Add the API typing and separate read-only view components.
- [ ] Re-run focused tests and frontend typecheck.

### Task 3: Full-height acquisition workspace

**Files:** `frontend/src/simple/SimpleLibraryApp.tsx`, `frontend/src/simple/simple.css`, `frontend/tests/simple-library.test.tsx`.

**Interfaces:** Sidebar navigation selects acquisition, holdings, or recommendations; existing book dialogs stay in place.

- [ ] Write failing frontend tests for sidebar navigation, preserved actions, and visible budget summary.
- [ ] Run focused Vitest and confirm the new behavior fails.
- [ ] Move fixed controls and budget summary into the left menu; give the main table remaining viewport height; reduce book-row height while preserving text size and access to details.
- [ ] Re-run frontend tests, typecheck, lint, and build.

### Task 4: Browser and documentation verification

**Files:** `docs/user-guide.md`, `README.md` as needed.

- [ ] Verify layout at 1366×768, 1920×1080, and narrow window widths with a populated list.
- [ ] Check keyboard navigation, search, source browsing, import, and selection against the design.
- [ ] Update user-facing layout instructions that reference the old budget cards.
- [ ] Run the required suites once more and inspect the diff for unintended files.
