# Frontend architecture and design

Next.js 16 (App Router, Cache Components on), TypeScript strict, Tailwind v4, shadcn/ui (Base UI
primitives), TanStack Query. Light theme only in Phase 1.

## Data and auth

- **Typed client.** `frontend/openapi.json` is exported from the backend
  (`uv run python -m scripts.export_openapi ../frontend/openapi.json`) and
  `npm run gen:api` turns it into `src/lib/api/schema.d.ts`. `openapi-fetch` uses those types, so a
  changed API contract breaks the frontend build. CI regenerates both and fails on a diff.
- **Session.** The access token lives in module memory (`src/lib/api/client.ts`), never in
  storage. On load, `AuthProvider` calls `POST /auth/refresh` (httpOnly cookie) to restore the
  session. Any 401 triggers one shared refresh and a single retry — concurrent requests never
  present the same refresh token twice, which the server would treat as theft (ADR 0001).
- **Route protection is client-side** (the token is not visible to the Next server). The data
  itself is protected by the API: tenant isolation is enforced there (ADR 0002), and the UI's
  "Organization not found" mirrors the API's 404.
- **Cache Components.** Org routes are dynamic (`/orgs/[orgId]/...`) and read the param with
  `useParams` in a client component, which suspends during prerender; the layout wraps the shell in
  `<Suspense>` whose fallback is the shell frame at its final size. Next keeps the previous route
  mounted but hidden for back navigation — tests must target visible elements.

## Design system ("framed sheet")

Dealt with the design-taste skill (app style `framed-sheet`, seed 148330720) and adopted whole:

- One tinted L-frame — sidebar (228px) and 48px top bar on `--ground` (#f2f5f7, a blue-grey step
  chosen as the product's face) with no rule between them; the page is a white sheet inset 8px from
  the right and bottom edges, radius 16, 1px edge, no shadow. Below 1024px the frame folds: a 48px
  bar with a menu button opens the navigation as a sheet.
- Colour on data and on the one primary action only: near-black primary button, grey chrome, the
  teal `--accent-data` (#1b6c80) reserved for data (Phase 3+) and the product mark.
- IBM Plex Sans (400/500/600) for everything; IBM Plex Mono only for slugs and IDs. Sizes
  12 / 13 / 14 / 22px. Tabular figures on dates and numbers.
- Lists are rules and alignment: 12px grey sentence-case headers, no fill band, 1px row rules, the
  identity column widest, dates right-aligned in ISO form, one status mark per row.
- States inside the block: loading keeps the final layout with flat grey bars; errors name what
  failed with the request id and a Retry; empty keeps the header and one sentence with the action.
- Honest data: every project shows "No telemetry yet" because OTLP ingestion arrives in Phase 3.

Checks run on the final build (`.taste-skill/` holds the artifacts, not committed): the skill's
linter and inspector at 1440/768/390 — 0 FAIL. Remaining warnings, accepted: the white sheet below
a short list is the page's end, not a stretched card; 12px sublines/pills on the phone; and a
font-fallback warning that only occurs in the script-free snapshot used to inspect the signed-in
screen (the live login page measured IBM Plex at 100%).
