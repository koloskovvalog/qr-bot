# ShirpDrop CS2 skin provider evaluation

Status: integration foundation only. No deposits, withdrawals, Steam transfers, or real wallet credits are enabled by this change.

## Current comparison (official public documentation, 2026-11-27)

| Capability | CS2Sell | SkinsNode |
|---|---|---|
| Customer skin deposits | Yes, partner deposits must be enabled for the API account | Yes, after commercial onboarding |
| Customer Steam delivery | Yes, instant listings can be ordered to a customer's Trade URL | Yes, catalog item withdrawals to a customer's Trade URL |
| Deposit settlement | Credit the partner after Steam trade protection; reverse/refund events must be handled | Credit only after `creditStatus=available` and a wallet transaction ID; reversals must be handled |
| Withdrawals | Orders API, instant listings only, up to 20 listings per order | Withdrawals/orders API; customer is asked to accept the offer within 10 minutes |
| Authentication | Bearer API key with explicit scopes | Client ID + client secret |
| Webhooks | HMAC-SHA256 signature; delivery ID supports duplicate suppression | Signed webhook; deposits and withdrawals use different event envelopes |
| Publicly stated commercial fees | No partner fee schedule found in public API docs; ask sales/support | Marketing states $0 monthly fee and no charge on failed withdrawals; per-item spread/fees still require onboarding confirmation |

Sources:
- https://cs2sell.com/api-docs
- https://skinsnode.com/docs/deposits
- https://skinsnode.com/docs/withdrawals
- https://account.skinsnode.com/documentation

## Initial recommendation

Keep both adapters as candidates until each provider gives ShirpDrop written onboarding approval and a test account.

- CS2Sell is a strong candidate because one API exposes partner deposits and customer orders. Partner deposits are explicitly permission-gated. Its deposit payout is not spendable for API orders until the relevant settlement/balance rules allow it.
- SkinsNode has a clearly documented deposit quote → deposit → settlement flow and a separate withdrawals balance. It requires onboarding and a public HTTPS webhook. It documents a 10-minute acceptance window for withdrawal offers.
- Do not select based on marketing claims alone. Compare actual net payout per representative skin, withdrawal markup, settlement timing, reversals, minimum balances, source-IP restrictions, supported regions, KYC/business requirements and dispute handling.

## Environment variables (server-side only)

CS2Sell:
- `CS2SELL_API_KEY`: API key. Keep it in Render secret environment variables; never expose it to browser JavaScript.
- `CS2SELL_WEBHOOK_SECRET`: webhook HMAC secret.
- `CS2SELL_PARTNER_DEPOSITS_CONFIRMED=true`: set only after CS2Sell explicitly enables partner deposits and the key has deposits:read + deposits:write scopes.

SkinsNode:
- `SKINSNODE_CLIENT_ID`
- `SKINSNODE_CLIENT_SECRET`
- `SKINSNODE_DEPOSITS_CONFIRMED=true`: set only after provider confirms deposit access.
- `SKINSNODE_WITHDRAWALS_CONFIRMED=true`: set only after provider confirms withdrawal access.

Never paste secrets into chat or commit them to GitHub. Set them in Render → ShirpDrop service → Environment.

## Readiness endpoint

The cases-store admin API exposes `GET /api/admin/cs2-providers` (requires valid Telegram admin initData and `X-Store-Mode: cases`). It performs read-only credential checks:
- CS2Sell: `GET /v1/public/me`, reports granted scopes.
- SkinsNode: `GET /v1/wallet`, does not expose provider balances or account identifiers.

The endpoint does not initiate a trade or move funds. Readiness flags require explicit provider onboarding confirmation via the environment flags above.

## Required transaction invariants before activation

1. Verify Steam identity using Steam OpenID; never collect Steam passwords or Steam Guard codes.
2. Validate and bind the user's SteamID64 to their Trade URL.
3. Persist each provider operation with a stable external ID before calling the provider.
4. Verify webhook signatures against the raw request body and deduplicate provider event IDs.
5. Treat HTTP 202 as accepted/processing, never as completed.
6. Credit the user's RUB wallet only once, after final settlement is confirmed by the provider. Keep a separate external-operation ledger and reverse credits if a provider reports a valid reversal.
7. Reserve wallet funds before withdrawal; release them on a final failure/cancellation, never on a transient error.
8. Keep deposit, withdrawal and existing demo inventory/wallet histories distinct. Do not delete or rewrite historical records.
9. Add provider polling as recovery for missed webhooks, with bounded retries and rate limits.
10. Keep real-money random case/upgrader functionality disabled until the applicable legal and licensing requirements are confirmed.

## Current implementation boundary

This change adds additive PostgreSQL tables for Steam identity, deposits, withdrawals, provider webhook idempotency and external wallet ledger, plus an admin-only read-only provider connectivity endpoint. It deliberately does not enable money-moving calls until partner access, test credentials, webhook contracts and commercial terms are confirmed.
