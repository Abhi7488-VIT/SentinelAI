# SentinelAI frontend

Next.js 16 (App Router) + TypeScript + Tailwind v4 + shadcn/ui + TanStack Query.
See `../docs/architecture/frontend.md` for the data flow and design rules, and the root
`README.md` for running the whole stack.

```bash
npm ci
npm run dev          # http://localhost:3000 (expects the API at NEXT_PUBLIC_API_URL)
npm run lint
npm run typecheck    # next typegen + tsc
npm test             # vitest (component + client tests)
npm run build
npm run gen:api      # regenerate src/lib/api/schema.d.ts from openapi.json
npm run e2e          # playwright, against a running stack (docker compose up)
```
