**CONSTRAINT: Your final deliverable is a file named `REPORT.json` in the current directory, containing one JSON
object with exactly the 18 keys listed at the end of this task. Write it before you finish.**

# Incident: checkout failure for order {ORDER}

At 2026-09-17 14:32 UTC a customer's checkout for order `{ORDER}` failed. The browser received:

```
HTTP 502 Bad Gateway  (POST /api/checkout, order {ORDER})
```

Logs from the four services involved are in `logs/`:

- `logs/gateway.log` – API gateway
- `logs/orders.log` – orders service
- `logs/inventory.log` – inventory service
- `logs/payments.log` – payments service

Each log is large (roughly 15,000–25,000 tokens) and noisy, and contains many unrelated errors. Services do not
log each other's IDs: each service tags its lines with its own identifiers, and a line in one service tells you
which identifier to look for in the next. Follow this specific failed request from the gateway to the component
that actually caused it.

## REPORT.json

Answer each question below with the value exactly as it appears in the logs. Use JSON strings for IDs, names,
paths and timestamps, and JSON integers for counts and numbers.

1. `gateway_request_id` — the gateway's request ID (`req=`) of the failed POST /api/checkout for {ORDER}.
2. `upstream_trace_id` — the trace ID the gateway recorded for the upstream call of that request.
3. `hold_id` — the inventory hold that the orders service was waiting on for that trace.
4. `worker` — the inventory worker that hold was queued on.
5. `root_cause_component` — the software component that caused the failure (a class name, not a service or worker
   name), named exactly as it is spelled in the logs.
6. `payment_intent_id` — the payment intent created for {ORDER}.
7. `gateway_status_code` — the HTTP status code the gateway returned for that request.
8. `upstream_timeout_ms` — the timeout in milliseconds the gateway reported for the upstream call.
9. `retry_attempts` — how many times the orders service retried confirming the hold for that trace.
10. `blocking_hold_id` — the hold the worker was busy with when it stalled.
11. `stall_started_at` — the timestamp of the log line where the worker stalled.
12. `payment_intent_final_status` — the last status recorded for that payment intent.
13. `gateway_latency_ms` — the latency in milliseconds the gateway logged when it returned the error for that
    request.
14. `item_sku` — the SKU that the orders service tried to reserve for that trace.
15. `payment_amount` — the amount of that payment intent, as a string exactly as written in the logs (for example
    `"123.45"`).
16. `queue_depth` — the queue depth reported when that hold was queued.
17. `retry_request_id` — the gateway request ID of the customer's later checkout attempt for the same order.
18. `hold_expired_at` — the timestamp of the log line where that hold expired.

Example shape (values are placeholders):

```json
{"gateway_request_id": "gw-000000", "upstream_trace_id": "tr-00000000", "hold_id": "hold-000000",
 "worker": "inv-w0", "root_cause_component": "Name", "payment_intent_id": "pi_0000000000",
 ...the twelve other keys...}
```

Scoring: each of the 18 keys is checked by an automated test and is worth 1 point.
