#!/usr/bin/env python3
"""Generate four noisy service logs with one failed checkout to trace, plus the expected report.

Usage: python3 gen_logs.py WORKSPACE_DIR [--seed 1] [--order ORD-77341]

Writes WORKSPACE_DIR/logs/{gateway,orders,inventory,payments}.log and prints the expected
REPORT.json values as JSON on stdout. Each seed changes every identifier, the worker, the
root-cause class, the retry count and the noise, so the agent cannot memorize answers.
"""
import argparse
import datetime
import json
import os
import random

ROOT_CAUSES = [
    ("ReservationReconciler", "applyBatch", "deadlock detected while reconciling reservations"),
    ("StockLedgerLock", "acquire", "lock wait never returned, ledger row locked"),
    ("SkuShardRouter", "route", "shard map refresh blocked the worker thread"),
    ("HoldExpiryScheduler", "sweep", "sweep loop stuck holding the worker"),
    ("BinLocationIndex", "rebuild", "index rebuild froze the worker"),
]
DISTRACTOR_CLASSES = ["PriceCacheClient", "SkuMetadataLoader", "WarehouseFeedParser", "AuditTrailWriter",
                      "PromoRulesEngine", "TaxRateLookup"]


class Clock:
    def __init__(self, start):
        self.start = start

    def at(self, seconds):
        stamp = self.start + datetime.timedelta(seconds=seconds)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (stamp.microsecond // 1000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--order", default="ORD-77341")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    clock = Clock(datetime.datetime(2026, 9, 17, 14, 20, 0))
    logs = {"gateway": [], "orders": [], "inventory": [], "payments": []}
    used = set()

    def uid(fmt, fn):
        while True:
            value = fmt % fn()
            if value not in used:
                used.add(value)
                return value

    gw_id = lambda: uid("gw-%06d", lambda: rng.randint(100000, 999999))
    trace_id = lambda: uid("tr-%08x", lambda: rng.getrandbits(32))
    hold_id = lambda: uid("hold-%06d", lambda: rng.randint(100000, 999999))
    intent_id = lambda: uid("pi_%010d", lambda: rng.randint(10 ** 9, 10 ** 10 - 1))
    order_id = lambda: uid("ORD-%05d", lambda: rng.randint(10000, 99999))
    sku = lambda: "SKU-%04d" % rng.randint(1000, 9999)
    workers = ["inv-w%d" % i for i in range(8)]

    def emit(service, t, level, text):
        logs[service].append((t, "%s %-5s %s %s" % (clock.at(t), level, service, text)))

    # ---- the failing request -------------------------------------------------------------
    order = args.order
    t0 = 12 * 60 + rng.uniform(1, 50)          # around 14:32
    target_gw, target_trace, target_hold, target_intent = gw_id(), trace_id(), hold_id(), intent_id()
    stuck_worker = rng.choice(workers)
    blocker_hold = hold_id()
    cause_class, cause_method, cause_text = rng.choice(ROOT_CAUSES)
    timeout_ms = rng.choice([25000, 30000])
    retries = rng.randint(3, 6)
    stall_t = t0 - rng.uniform(20, 60)
    amount = "%d.%02d" % (rng.randint(40, 400), rng.randint(0, 99))
    item_sku = sku()

    emit("gateway", t0, "INFO", 'req=%s method=POST path=/api/checkout order=%s client=203.0.113.%d msg="request received"'
         % (target_gw, order, rng.randint(2, 250)))
    emit("gateway", t0 + 0.003, "INFO", 'req=%s upstream=orders-svc trace=%s msg="forwarding to upstream"' % (target_gw, target_trace))
    emit("orders", t0 + 0.006, "INFO", "trace=%s checkout started order=%s items=%d" % (target_trace, order, rng.randint(1, 4)))
    emit("payments", t0 + 0.15, "INFO", "intent=%s created order=%s amount=%s currency=USD" % (target_intent, order, amount))
    emit("payments", t0 + 0.21, "INFO", "intent=%s status=requires_capture" % target_intent)
    emit("orders", t0 + 0.22, "INFO", "trace=%s payment intent ready, reserving stock" % target_trace)
    emit("orders", t0 + 0.25, "INFO", "trace=%s reserve requested hold=%s sku=%s qty=%d" % (target_trace, target_hold, item_sku, rng.randint(1, 3)))
    emit("inventory", t0 + 0.27, "INFO", "hold=%s created sku=%s" % (target_hold, item_sku))
    queue_depth = rng.randint(9, 30)
    emit("inventory", t0 + 0.28, "INFO", "hold=%s queued worker=%s queue_depth=%d" % (target_hold, stuck_worker, queue_depth))
    step = (timeout_ms / 1000.0 - 1.0) / retries
    for attempt in range(1, retries + 1):
        emit("orders", t0 + 0.3 + step * attempt, "WARN",
             "trace=%s hold=%s still pending, retrying confirmation (attempt %d/%d)" % (target_trace, target_hold, attempt, retries))
    emit("orders", t0 + timeout_ms / 1000.0 - 0.4, "ERROR",
         "trace=%s giving up: hold=%s not confirmed after %d attempts, aborting checkout" % (target_trace, target_hold, retries))
    emit("orders", t0 + timeout_ms / 1000.0 - 0.35, "INFO", "trace=%s requesting cancel of payment for order=%s" % (target_trace, order))
    emit("payments", t0 + timeout_ms / 1000.0 - 0.3, "INFO", "intent=%s canceled reason=order_aborted" % target_intent)
    emit("gateway", t0 + timeout_ms / 1000.0, "ERROR",
         'req=%s upstream=orders-svc trace=%s msg="upstream timed out" timeout_ms=%d' % (target_gw, target_trace, timeout_ms))
    latency_ms = timeout_ms + rng.randint(2, 9)
    emit("gateway", t0 + timeout_ms / 1000.0 + 0.002, "ERROR",
         'req=%s status=502 msg="Bad Gateway" latency_ms=%d' % (target_gw, latency_ms))
    expired_stamp = clock.at(t0 + timeout_ms / 1000.0 + 5)
    emit("inventory", t0 + timeout_ms / 1000.0 + 5, "WARN", "hold=%s expired before processing worker=%s" % (target_hold, stuck_worker))

    # the stuck worker: it picked up the blocker hold, then the root-cause class stalled
    emit("inventory", stall_t - 0.5, "INFO", "worker=%s picked job hold=%s" % (stuck_worker, blocker_hold))
    stall_stamp = clock.at(stall_t)
    emit("inventory", stall_t, "ERROR", "worker=%s job=%s %s in %s.%s, worker stalled" % (
        stuck_worker, blocker_hold, cause_text, cause_class, cause_method))
    emit("inventory", stall_t + 0.001, "ERROR", "    at com.acme.inventory.%s.%s(%s.java:%d)" % (
        cause_class, cause_method, cause_class, rng.randint(80, 400)))
    emit("inventory", stall_t + 0.002, "ERROR", "    at com.acme.inventory.WorkerLoop.run(WorkerLoop.java:57)")
    t = stall_t + 10
    while t < t0 + timeout_ms / 1000.0 + 60:
        emit("inventory", t, "WARN", "worker=%s heartbeat missed, busy with job=%s for %ds" % (
            stuck_worker, blocker_hold, int(t - stall_t)))
        t += 10

    # a later retry of the same order that succeeds (distractor: different request and trace)
    t1 = t0 + timeout_ms / 1000.0 + rng.uniform(40, 90)
    retry_gw, retry_trace, retry_hold = gw_id(), trace_id(), hold_id()
    other_worker = rng.choice([w for w in workers if w != stuck_worker])
    emit("gateway", t1, "INFO", 'req=%s method=POST path=/api/checkout order=%s client=203.0.113.9 msg="request received"' % (retry_gw, order))
    emit("gateway", t1 + 0.003, "INFO", 'req=%s upstream=orders-svc trace=%s msg="forwarding to upstream"' % (retry_gw, retry_trace))
    emit("orders", t1 + 0.01, "INFO", "trace=%s checkout started order=%s items=1" % (retry_trace, order))
    emit("orders", t1 + 0.05, "INFO", "trace=%s reusing payment intent for order=%s" % (retry_trace, order))
    emit("orders", t1 + 0.06, "INFO", "trace=%s reserve requested hold=%s sku=%s qty=1" % (retry_trace, retry_hold, item_sku))
    emit("inventory", t1 + 0.07, "INFO", "hold=%s created sku=%s" % (retry_hold, item_sku))
    emit("inventory", t1 + 0.08, "INFO", "hold=%s queued worker=%s queue_depth=2" % (retry_hold, other_worker))
    emit("inventory", t1 + 0.4, "INFO", "worker=%s confirmed hold=%s" % (other_worker, retry_hold))
    emit("orders", t1 + 0.5, "INFO", "trace=%s hold=%s confirmed" % (retry_trace, retry_hold))
    emit("orders", t1 + 0.6, "ERROR", "trace=%s payment step failed: card_declined, checkout ended" % retry_trace)
    emit("gateway", t1 + 0.7, "INFO", 'req=%s status=402 msg="Payment Required" latency_ms=700' % retry_gw)
    emit("gateway", t0 - 400, "INFO", 'req=%s method=GET path=/api/orders/%s status=200 latency_ms=12' % (gw_id(), order))

    # ---- noise: other checkouts, many of them failing for unrelated reasons ----------------
    similar = order[:-2] + order[-1] + order[-2]   # e.g. ORD-77314 next to ORD-77341
    if similar == order:                            # last two digits equal: change the last digit instead
        similar = order[:-1] + str((int(order[-1]) + 3) % 10)
    for i in range(170):
        start = rng.uniform(0, 30 * 60)
        o = similar if i == 0 else order_id()
        g, tr, h, pi = gw_id(), trace_id(), hold_id(), intent_id()
        w = rng.choice(workers)
        if i == 0:
            start = t0 + rng.uniform(-30, 30)
        if w == stuck_worker and start > stall_t - 5:
            # a stalled worker processes nothing until the incident is over
            w = rng.choice([x for x in workers if x != stuck_worker])
        outcome = rng.random()
        emit("gateway", start, "INFO", 'req=%s method=POST path=/api/checkout order=%s client=198.51.100.%d msg="request received"' % (g, o, rng.randint(2, 250)))
        emit("gateway", start + 0.003, "INFO", 'req=%s upstream=orders-svc trace=%s msg="forwarding to upstream"' % (g, tr))
        emit("orders", start + 0.006, "INFO", "trace=%s checkout started order=%s items=%d" % (tr, o, rng.randint(1, 5)))
        emit("payments", start + 0.1, "INFO", "intent=%s created order=%s amount=%d.%02d currency=USD" % (pi, o, rng.randint(5, 900), rng.randint(0, 99)))
        emit("orders", start + 0.2, "INFO", "trace=%s reserve requested hold=%s sku=%s qty=%d" % (tr, h, sku(), rng.randint(1, 3)))
        emit("inventory", start + 0.22, "INFO", "hold=%s created sku=%s" % (h, sku()))
        emit("inventory", start + 0.23, "INFO", "hold=%s queued worker=%s queue_depth=%d" % (h, w, rng.randint(0, 12)))
        if outcome < 0.55:
            emit("inventory", start + 0.5, "INFO", "worker=%s confirmed hold=%s" % (w, h))
            emit("orders", start + 0.6, "INFO", "trace=%s hold=%s confirmed" % (tr, h))
            emit("payments", start + 0.8, "INFO", "intent=%s captured" % pi)
            emit("orders", start + 0.9, "INFO", "trace=%s checkout completed order=%s" % (tr, o))
            emit("gateway", start + 1.0, "INFO", 'req=%s status=200 msg="OK" latency_ms=%d' % (g, rng.randint(400, 1200)))
        elif outcome < 0.7:
            emit("inventory", start + 0.5, "INFO", "worker=%s confirmed hold=%s" % (w, h))
            emit("orders", start + 0.6, "INFO", "trace=%s hold=%s confirmed" % (tr, h))
            emit("payments", start + 0.8, "ERROR", "intent=%s failed reason=%s" % (pi, rng.choice(["card_declined", "insufficient_funds", "expired_card"])))
            emit("orders", start + 0.9, "ERROR", "trace=%s payment step failed, checkout ended" % tr)
            emit("gateway", start + 1.0, "INFO", 'req=%s status=402 msg="Payment Required" latency_ms=%d' % (g, rng.randint(400, 1200)))
        elif outcome < 0.85:
            cls = rng.choice(DISTRACTOR_CLASSES)
            emit("inventory", start + 0.4, "ERROR", "worker=%s job=%s %s threw IllegalStateException in %s.load, job failed" % (w, h, "unexpected state", cls))
            emit("inventory", start + 0.401, "ERROR", "    at com.acme.inventory.%s.load(%s.java:%d)" % (cls, cls, rng.randint(20, 300)))
            emit("orders", start + 0.6, "ERROR", "trace=%s hold=%s rejected by inventory, returning 409" % (tr, h))
            emit("payments", start + 0.7, "INFO", "intent=%s canceled reason=order_aborted" % pi)
            emit("gateway", start + 0.8, "WARN", 'req=%s status=409 msg="Conflict" latency_ms=%d' % (g, rng.randint(400, 900)))
        else:
            emit("orders", start + 0.4, "ERROR", "trace=%s database connection reset by peer, aborting" % tr)
            emit("payments", start + 0.5, "INFO", "intent=%s canceled reason=order_aborted" % pi)
            emit("gateway", start + 0.6, "ERROR", 'req=%s upstream=orders-svc trace=%s msg="upstream connection reset"' % (g, tr))
            emit("gateway", start + 0.601, "ERROR", 'req=%s status=502 msg="Bad Gateway" latency_ms=%d' % (g, rng.randint(400, 900)))

    # background chatter
    for _ in range(260):
        t = rng.uniform(0, 30 * 60)
        service = rng.choice(list(logs))
        line = rng.choice([
            ("INFO", "healthcheck ok latency_ms=%d" % rng.randint(1, 20)),
            ("INFO", "config refresh completed version=%d" % rng.randint(100, 999)),
            ("DEBUG", "gc pause_ms=%d heap_mb=%d" % (rng.randint(2, 90), rng.randint(300, 2000))),
            ("WARN", "slow query took %dms table=%s" % (rng.randint(300, 4000), rng.choice(["orders", "holds", "intents", "sessions"]))),
            ("ERROR", "metrics exporter push failed: connection refused"),
            ("INFO", "cache warmup finished entries=%d" % rng.randint(1000, 90000)),
        ])
        emit(service, t, line[0], line[1])
    for w in workers:
        if w == stuck_worker:
            continue
        for _ in range(12):
            t = rng.uniform(0, 30 * 60)
            emit("inventory", t, "INFO", "worker=%s heartbeat ok jobs_done=%d" % (w, rng.randint(50, 900)))

    os.makedirs(os.path.join(args.workspace, "logs"), exist_ok=True)
    for service, entries in logs.items():
        entries.sort(key=lambda e: e[0])
        with open(os.path.join(args.workspace, "logs", service + ".log"), "w", encoding="utf-8") as f:
            f.write("\n".join(text for _, text in entries) + "\n")

    expected = {
        "gateway_request_id": target_gw,
        "upstream_trace_id": target_trace,
        "hold_id": target_hold,
        "worker": stuck_worker,
        "root_cause_component": cause_class,
        "payment_intent_id": target_intent,
        "gateway_status_code": 502,
        "upstream_timeout_ms": timeout_ms,
        "retry_attempts": retries,
        "blocking_hold_id": blocker_hold,
        "stall_started_at": stall_stamp,
        "payment_intent_final_status": "canceled",
        "gateway_latency_ms": latency_ms,
        "item_sku": item_sku,
        "payment_amount": amount,
        "queue_depth": queue_depth,
        "retry_request_id": retry_gw,
        "hold_expired_at": expired_stamp,
    }
    print(json.dumps(expected))


if __name__ == "__main__":
    main()
