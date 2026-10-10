# Template Guide

This guide describes reusable starter facilities. Current requirements govern inherited code: preserve unrelated correct work and correct contradictions with those requirements.

## Project layout

- `frontend/src/App.tsx` holds routes; `frontend/src/pages/` holds pages; `frontend/src/api/index.ts` exports the default HTTP client.
- `backend/src/app.js` registers `/api` routes and serves the built frontend; `backend/src/index.js` starts the single server on `PORT` or 3000.
- `backend/src/database/init_db.js` owns schema initialization; `backend/src/database/db_runtime.js` exports database operations.

## Build and start

From the project root, install each package independently if dependencies are missing:

```sh
cd backend && npm install
cd ../frontend && npm install
```

Build the frontend before starting the backend:

```sh
cd frontend && npm run build
cd ../backend && npm run start
```

The backend serves the frontend and API on one port. `GET /api/health` checks that it is ready.

## Existing capabilities

- Import the default client from `frontend/src/api/index.ts` in frontend code instead of creating another API client. It already targets `/api`.
- Import `run`, `get`, `all`, `exec`, or `withTransaction` from `backend/src/database/db_runtime.js` for database operations. `run` returns `{ lastID, changes }`; `get` returns a row or `null`; `all` returns rows. Use the returned result, not an assumed outcome, when reporting a write or lookup.
- Public reads/writes and transactions share one queue. Inside `withTransaction`, use only the supplied `tx`; public calls and nested transactions reject immediately. Closing/changing the database is a lifecycle operation when business work is idle. Bare `getDb()` is outside this concurrency guarantee.
- The seed entrypoint is intentionally empty. Startup awaits `seedDatabase` after schema initialization and before listening; a seed failure stops startup. Generate only missing required records and preserve successful user changes on restart. Test preparation does not initialize production data.
- Keep table creation and compatible schema changes in the existing `backend/src/database/init_db.js` initialization path. Inspect existing tables and code before editing.
- React, Vite, Tailwind CSS, Express, SQLite, tests, and the two public control packages are already declared by the template.

## Public control examples

The following are neutral composition patterns, not prebuilt screens or task behavior. Use native `<button>` and `<input>` elements with existing Tailwind utilities for ordinary controls.

Dialog import path: `@radix-ui/react-dialog`.

```tsx
import * as Dialog from '@radix-ui/react-dialog';

<Dialog.Root>
  <Dialog.Trigger asChild><button type="button">Open</button></Dialog.Trigger>
  <Dialog.Portal>
    <Dialog.Overlay className="fixed inset-0 bg-black/40" />
    <Dialog.Content className="fixed left-1/2 top-1/2 bg-white p-4">
      <Dialog.Title>Details</Dialog.Title>
      <Dialog.Description>Review this information.</Dialog.Description>
      <Dialog.Close asChild><button type="button">Close</button></Dialog.Close>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
```

Dropdown menu import path: `@radix-ui/react-dropdown-menu`.

```tsx
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

<DropdownMenu.Root>
  <DropdownMenu.Trigger asChild><button type="button">Options</button></DropdownMenu.Trigger>
  <DropdownMenu.Portal>
    <DropdownMenu.Content className="bg-white p-2 shadow">
      <DropdownMenu.Item onSelect={handleAction}>Action</DropdownMenu.Item>
    </DropdownMenu.Content>
  </DropdownMenu.Portal>
</DropdownMenu.Root>
```

`DropdownMenu.Item` assigns `menuitem` semantics even with `asChild`. When a requirement calls for a link, use a native `<a href>` or router `<Link>` in a link-preserving popover/container; do not wrap that link in `DropdownMenu.Item`.

For an error, render ordinary accessible text near the affected control (for example, `<p role="alert">{errorMessage}</p>`). Choose the actual message and handling from the current requirements; no business error code is built in.

## Test commands

From `frontend/`: `npm run test` and `npm run build`.
From `backend/`: `npm run test`; `npm run test:e2e` runs Playwright tests when its browser dependencies are available. `npm run test:all` runs both backend test groups.
The E2E（端到端）groups require generated real tests and a runnable service; an empty directory is not acceptance success.
Test preparation requires an explicit independent path: `npm run db:prepare:e2e -- --db-path .arc-test-db/e2e.sqlite`. Runtime/default database paths and their symlink aliases are rejected.

## Implementation boundary

Reuse existing dependencies and utilities where they fit. Implement business objects, pages, data, wording, and permissions from the current requirement subtree. Do not infer task behavior from this guide or replace existing work to make it match the guide. If code and guide disagree, inspect the code and verify the relevant behavior first.

## Shared state and interfaces

The host sends the state/interface rules with each request. The minimum for this project:

- Before changing a shared value or object shape, read its definition and its real callers. Keep one representation and update every consumer; do not guess property names or add a second near-identical shape.
- Editable controls keep the committed value, the draft value and the draft's original owner distinct. A focus or selection change without user edits must not write data. Commit a dirty draft once to its captured owner, clear the dirty state before another event can submit, and keep the last successful state when a write fails.
- Control role and accessible name come from the current requirements, not from a visually similar component. Check dependent display, export, history and structural updates against the same raw/derived representation, and keep valid zero, false and empty values distinct from missing ones.
- Keep the module's existing shared interfaces unless a requirement forces a change, and then update the real callers in the same turn. Do not leave an unconnected helper or a consumer reading an old field name.
