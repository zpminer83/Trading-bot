# dreamDEX trading-status authority audit

Audit date: 2026-07-18  
Repository commit reviewed: `43ef0e8`  
Decision: **authoritative source not found (B)**

## Scope and safety

This was a local source/fixture audit only. No login, refresh, network request,
signing, transaction, mutation RPC, journal write, or credential read was
performed. No speculative selector or generic `eth_call` adapter was added.

## Source inventory

Reviewed:

- `bot/integrations/dreamdex_market_rules.py`
- `bot/integrations/dreamdex_read_only.py`
- `bot/execution/dreamdex_zero_mutation_rehearsal.py`
- `bot/execution/dreamdex_readonly_rpc.py`
- authenticated account models, parsers, transports, and schema fixtures;
- public market and order-book fixtures;
- direct-owner execution/readiness modules and tests;
- `vendor/dreamdex-bot-kit` TypeScript ABI/source, generated type helpers,
  Python ABI/helpers, pool interface, execution client, and configuration
  artifacts.

## Candidate source table

| Source | Field/function | Surface | Authority conclusion |
|---|---|---|---|
| Public market parser | `status` | public market metadata | Ambiguous without documented mapping; missing in the observed live schema. |
| Public market parser | `tradingEnabled` / `enabled` | public market metadata | Accepted only when explicitly present and boolean; not present in the observed live response. |
| Public order book | bids/asks | public market data | Not a lifecycle or trading-enabled signal. |
| Authenticated account snapshot | balances/orders/fills status | authenticated account | Account evidence only; no confirmed market lifecycle, place, or cancel permission field. |
| Bot Kit `SPOT_POOL_ABI` | `getPoolParams()` | on-chain view | Rules/fees/tick/lot/minimum metadata; no lifecycle or enable flag. |
| Bot Kit `SPOT_POOL_ABI` | `getBookLevels(bool,uint64)` | on-chain view | Book levels only; not trading status. |
| Bot Kit `SPOT_POOL_ABI` | `getOwnOpenOrders()` / `getOrder(uint128)` | on-chain view | Order state only; not market lifecycle. |
| Bot Kit `SPOT_POOL_ABI` | `getAutoPullRequirement(...)` | on-chain view | Funding requirement only; not lifecycle. |
| Bot Kit execution client | `placeOrder` / `placeOrderFor` | transaction path | Input guards and simulation are present, but no explicit authoritative lifecycle check. State-changing path; not used by rehearsal. |
| Bot Kit execution client | `cancelOrder` / `cancelOrderFor` / `reduceOrder` | transaction path | State-changing operations; no read-only support-status source. |
| Contract ABI/source search | `paused`, `isPaused`, `tradingEnabled`, `marketStatus`, `poolStatus` | on-chain | No source-confirmed function and target mapping found in the reviewed pool ABI/source. |

No exact selector, ABI input/output pair, or deployed target mapping was found
for a market lifecycle view. Function names in examples and dependencies were
not treated as ABI evidence.

## Public schema conclusion

The parser preserves the distinction between:

- market listed;
- exact market identity;
- market rules;
- market lifecycle;
- trading enabled;
- place support;
- cancel support.

An absent field remains unavailable. A string status is not mapped to enabled
unless the source explicitly confirms its semantics. Order-book activity,
listing presence, and successful contract-code reads do not enable trading.

## Authenticated schema conclusion

Existing authenticated fixtures and parsers expose account balances, open
orders, fills, and source/pagination status. They do not provide a confirmed
market lifecycle or place/cancel permission field. No authentication was
performed to look for additional data.

## Final decision and blockers

The rehearsal keeps `confirmed_unavailable_from_source` for authoritative
trading status and propagates these separate blockers:

- `trading_status_authoritative_source_unavailable`
- `market_lifecycle_unconfirmed`
- `place_operation_support_unconfirmed`
- `cancel_operation_support_unconfirmed`

Candidate construction, gas estimation, approval preview, signer invocation,
and submission remain blocked when this evidence is unavailable.

No guessed selector, speculative contract adapter, or fallback based on weak
signals was introduced.
