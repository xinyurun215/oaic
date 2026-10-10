# Web Template

This template is a single-port web application. The frontend is built with Vite and React, and the backend is an Express server that serves both `/api/*` routes and the compiled frontend from `frontend/dist`.

Read [`TEMPLATE_GUIDE.md`](TEMPLATE_GUIDE.md) before the first code change. It identifies the existing API and database helpers, public control imports, and the build/test sequence. For a platform-prepared application without this root file, the agent prompt points to the bundled guide; inspect the prepared code before applying any example.

## Project Layout

- `frontend/`: React, Vite, Tailwind, Vitest, and frontend tests.
- `backend/`: Express, SQLite runtime helpers, backend Vitest tests, and Playwright E2E tests.
- `backend/src/app.js`: Express app, `/api/health`, static frontend hosting, and SPA fallback.
- `backend/src/index.js`: Backend process entrypoint.

## Prerequisites

- Node.js and npm.
- A shell environment that can install npm dependencies in both `frontend` and `backend`.

## Install Dependencies

Install each package independently:

```bash
cd backend
npm install

cd ../frontend
npm install
```

## Build and Run

The production-style runtime is backend-led. Build the frontend first, then start the backend:

```bash
cd frontend
npm run build

cd ../backend
npm run start
```

The backend listens on `PORT` when set, otherwise it uses the template port configured by ARC. After startup, the backend serves the compiled frontend and API routes from the same origin.

Health check:

```bash
curl http://127.0.0.1:<port>/api/health
```

## Development Commands

Backend development server:

```bash
cd backend
npm run dev
```

Frontend development server:

```bash
cd frontend
npm run dev
```

The Vite dev server proxies `/api` to the configured backend port.

## Tests

Frontend unit/integration tests:

```bash
cd frontend
npm run test
```

Backend unit/integration tests:

```bash
cd backend
npm run test
```

`npm run test` also runs the database initialization and transaction contract tests.

Backend E2E tests use Playwright and live under `backend/test-e2e`:

```bash
cd backend
npm run test:e2e
```

Run backend Vitest tests followed by E2E tests:

```bash
cd backend
npm run test:all
```

`test:e2e` and `test:all` require real generated E2E tests and a running service. The starter has no business E2E suite; no-tests is not a pass.

## Database Notes

- Runtime database helpers live under `backend/src/database`.
- The default database file is `database.db`, unless `ARC_DB_FILE` or `DATABASE_FILE` is set.
- Test helpers create isolated SQLite files under `.arc-test-db`.
- `npm run db:seed` runs the template seed entrypoint.
- `npm run db:prepare:e2e -- --db-path .arc-test-db/e2e.sqlite` requires an explicit independent test database. Default/runtime paths and real-path aliases are rejected; repeating preparation rebuilds only the chosen test file.
- Public operations share the transaction queue. Inside `withTransaction` use only its `tx`; nested transactions/public calls reject. Close/change the database only at idle lifecycle points; bare `getDb()` is not covered.
- Production seed is empty. Generated business initialization must be connected to production startup; preparing tests is not production provisioning.
