# Entry controls and order acknowledgements

**Applies to this review branch of `radwanmhh-svg/NexusTrading-System-MT5_CLEAN`, based on `11f7470`.** This is a source change for review, not a deployment, strategy approval, or broker integration test.

## What is authoritative?

`GET /api/system/mode` reads one transactional Redis snapshot. It reports the gateway's known execution mode, the requested AUTO flag, the effective control permission, the manual/risk holds, and `entry_block`. Permission is **not** a fill, or even proof the executor has a compatible account. The executor independently checks its own process mode and current account.

| Operation | Effect |
| --- | --- |
| No explicit execution mode / no AUTO | Defaults to `shadow` / disarmed. Neither the session governor's schedule nor saved browser preferences can authorize a send. Invalid or unreadable controls never grant entry. |
| `POST /api/system/mode` | Changes the execution mode and disarms AUTO atomically. A LIVE request requires the gateway's existing server-side PIN verifier and a matching process configuration. No UI decision changes an actual broker account. |
| `POST /api/system/toggle-auto-trade` | Disable: `{"enabled":false}`. Enable requires the **expected mode** as well as explicit confirmation, e.g. `{"enabled":true,"expected_mode":"paper"}`. Interlocks, a mode mismatch or a concurrent control change refuse the enable. The explicit mode and AUTO-enable leases expire after at most 24 hours. |
| `POST /api/system/kill-switch` | Server-verified PIN. Sets the operator latch (no TTL) and disarms AUTO in one transaction. This is **not** a liquidation endpoint. |
| `POST /api/system/kill-switch/release` | Server-verified PIN. Removes **only** the operator latch and leaves AUTO off. Does not clear risk/anti-tilt or reconciliation controls. |

Keep the gateway on a trusted, access-controlled interface. The PIN verifier is a private component of the source-only export; it was not replaced with a fallback. Configure the verifier privately before use. Do not distribute a `NEXT_PUBLIC_*` secret or commit a local `.env`.

Both REST and the risk-decision stream recheck controls at the last boundary before `BrokerAdapter.send_order`. Already authorized/in-flight submissions may still reach the venue; Redis and the venue are not a distributed transaction. Existing positions and resting orders are **not** closed or cancelled. Explicit close/modify endpoints are separate; the existing automated lifecycle loop retains its stop behavior. The operator latch has no application expiry, but Redis persistence, eviction and operational recovery remain deployment concerns.

## Account and compatibility checklist

- Review and update the gateway, executor, risk/agent services, ML service and dashboard together. **Do not mix control writers/readers across old and new versions** or apply this over an active trading installation.
- The adapter's `get_account_info()` must return `AccountInfo.execution_mode` from its authenticated current session. A display label, account name, UI selection, environment flag, or port number is not evidence. Unknown/mismatched accounts are refused. The included CCXT sandbox adapter attests paper only after a successful account read. The included IBKR adapter does not attest by guessing from its port.
- The private MT5 adapter is absent in the CLEAN export. Its owner must validate the account classification on the private adapter (e.g. its actual broker-reported demo vs real trade mode, from the **same session** used for submission). On a disconnect, account switch, unsupported or ambiguous response: return unknown; do not add an override or simply hard-code `paper`. Until reviewed, MT5 entries may correctly remain blocked.
- `session:state.mode` is a session schedule, not a control. `nexus:system:execution_mode` is the separate execution control. Legacy risk halts are still honored. Existing YAML/config data is not a source of authorization; control writes to the general preferences endpoint are refused. The UI starts unknown, polls and rereads after each control mutation.

## What counts as execution?

The ML status endpoint now exposes `last_execution` separately from `trades_approved`. A reported full or partial fill needs a recognized status, order ID, positive finite fill price and executed quantity. Pending, rejected, cancelled and unconfirmed are distinct. An HTTP 200 with `MIN_RR_REJECTED` is not a fill; a timeout is **unconfirmed**, not proof of rejection. Reconcile ambiguous orders instead of blindly retrying. A9 marks attempts independently of fills so a subsequent stream approval for the same attempt is not a fallback send. Its existing frequency budget is reserved conservatively; it is not a fill ledger.

No quote is manufactured to make an order pass. In particular, the autonomous ML caller in this export still does not supply a fresh tradable bid/ask: such a request will be rejected by the R:R check, and that rejection should now be visible. Not addressed in this batch: redesigning independent risk approval/ownership (A9 can still call REST before the stream decision), durable cross-client idempotency, reconciliation of pending fills over time, ML validation, or the accounting/history findings in the original audit. Do **not** use the review branch as a live-trading sign-off.

## Repeat the targeted checks, without a broker

Use an isolated checkout and an environment with the project's Python dependencies. The following regression tests use in-process ASGI, a recording adapter, `fakeredis`, and mocked internal HTTP; they do not require MT5, credentials, live Redis, or a production database. The route test supplies a strict verifier **only inside that test**. The old broad suite still depends on files intentionally omitted from the CLEAN export.

```sh
python -m pip install -r tests/requirements-controls.txt
python -m pytest -q tests/test_executor/test_confirmed_controls.py tests/test_executor/test_execution_receipts.py
cd dashboard
npm ci
npm run test:controls
npx tsc --noEmit
npm run build
```

The React tests mount actual controls in jsdom, test a 503 on disable, denial of a PIN, conflicting/malformed readbacks, stale polls, mode changes while confirming enable, and successful halt/release. None of these checks logs into a venue or places a real order.
