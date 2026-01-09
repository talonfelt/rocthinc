# rocthinc — app at `apps/roc-thinc/`

This folder contains the **rocthinc** app (anchored blocks, thinc + Python via Pyodide, paywall, and real-time Team).

## Deploy on Vercel (same GitHub repo)
1) **New Project** → pick THIS repo again → set **Root Directory** to `apps/roc-thinc`.
2) Environment Variables:
   - `NEXT_PUBLIC_SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `SQUARE_ACCESS_TOKEN`
   - `SQUARE_SUBSCRIPTION_PLAN_VARIATION_ID`
   - `SQUARE_WEBHOOK_SIGNATURE_KEY`
   - `SITE_URL` = `https://app.rocthinc.cc`
3) Domains → add `app.rocthinc.cc` (DNS CNAME: `app` → `cname.vercel-dns.com`).
4) Supabase (run once):
```sql
create table if not exists licenses (
  user_email text primary key,
  status text not null check (status in ('active','expired','none')) default 'none',
  plan text,
  updated_at timestamptz default now()
);
```
5) Square Webhook URL: `https://app.rocthinc.cc/api/square/webhook` (put signature key in `SQUARE_WEBHOOK_SIGNATURE_KEY`).

## Paywall
- **Run** (thinc/Python) and **Team** (real-time editing) require an active license.
- Notes/UI/theme editor work without a subscription.

## Code map (inside `apps/roc-thinc`)
- Canvas + blocks: `app/page.tsx`, `src/editor/*`
- **thinc** + `roc`: `src/engine/thinc/runThinc.ts`
- Python worker: `public/py.worker.js`, bridge: `src/engine/py/PythonRunner.ts`
- Theme editor: `src/ui/ThemeEditor.tsx`, tokens: `src/theme/*`
- Auth + paywall + webhooks: `src/ui/Profile.tsx`, `app/api/*`
