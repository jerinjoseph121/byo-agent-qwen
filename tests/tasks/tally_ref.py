#!/usr/bin/env python3
"""Reference solution for tests/tasks/tally_task.md. Used only to compute expected outputs in tests.

It is not part of the agent: the agent must write its own tally.py from the task text.
"""
import csv
import datetime
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")


def fail(message, code):
    sys.stderr.write("error: %s\n" % message)
    sys.exit(code)


def money(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def parse_args(argv):
    if not argv:
        fail("missing command", 2)
    command, rest = argv[0], argv[1:]
    options, positional = {"file": "transactions.csv", "json": False}, []
    i = 0
    while i < len(rest):
        word = rest[i]
        if word == "--json":
            options["json"] = True
        elif word in ("--file", "--category", "--from", "--to"):
            if i + 1 >= len(rest):
                fail("missing value for %s" % word, 2)
            options[word[2:]] = rest[i + 1]
            i += 1
        else:
            positional.append(word)
        i += 1
    return command, positional, options


def load(path):
    try:
        f = open(path, newline="", encoding="utf-8")
    except OSError:
        fail("cannot read %s" % path, 1)
    rows = []
    with f:
        reader = csv.reader(f)
        next(reader, None)
        for fields in reader:
            if len(fields) != 4:
                continue
            date_text, _, category, amount_text = [x.strip() for x in fields]
            try:
                if len(date_text) != 10:
                    raise ValueError
                date = datetime.date.fromisoformat(date_text)
                amount = Decimal(amount_text)
                if not amount.is_finite():
                    raise InvalidOperation
            except (ValueError, InvalidOperation):
                continue
            if not category:
                continue
            rows.append((date, category.lower(), amount))
    return rows


def parse_date(text):
    try:
        if len(text) != 10:
            raise ValueError
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        fail("invalid date %r" % text, 2)


def main(argv):
    command, positional, options = parse_args(argv)
    if command not in ("count", "total", "top", "range", "months", "avg", "refunds", "max", "categories"):
        fail("unknown command %s" % command, 2)
    n = None
    if command == "top":
        if len(positional) != 1 or not positional[0].isdigit() or int(positional[0]) < 1:
            fail("top needs a positive number", 2)
        n = int(positional[0])
    if command == "avg" and "category" not in options:
        fail("avg needs --category", 2)
    if command == "range":
        if "from" not in options or "to" not in options:
            fail("range needs --from and --to", 2)
        start, end = parse_date(options["from"]), parse_date(options["to"])
        if start > end:
            fail("--from is later than --to", 2)
    rows = load(options["file"])
    if command in ("count", "refunds", "months") and "category" in options:
        rows = [r for r in rows if r[1] == options["category"].strip().lower()]
    as_json = options["json"]

    if command == "count":
        print(json.dumps({"count": len(rows)}) if as_json else len(rows))
    elif command == "total":
        if "category" in options:
            rows = [r for r in rows if r[1] == options["category"].strip().lower()]
        total = sum((r[2] for r in rows), Decimal(0))
        print(json.dumps({"total": money(total)}) if as_json else money(total))
    elif command == "top":
        totals = {}
        for _, category, amount in rows:
            totals[category] = totals.get(category, Decimal(0)) + amount
        best = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
        if as_json:
            print(json.dumps([{"category": c, "total": money(a)} for c, a in best]))
        else:
            for category, amount in best:
                print("%s\t%s" % (category, money(amount)))
    elif command == "range":
        chosen = [r for r in rows if start <= r[0] <= end]
        total = sum((r[2] for r in chosen), Decimal(0))
        if as_json:
            print(json.dumps({"count": len(chosen), "total": money(total)}))
        else:
            print("count=%d total=%s" % (len(chosen), money(total)))
    elif command == "months":
        totals = {}
        for date, _, amount in rows:
            key = date.strftime("%Y-%m")
            totals[key] = totals.get(key, Decimal(0)) + amount
        for key in sorted(totals):
            print("%s\t%s" % (key, money(totals[key])))
    elif command == "avg":
        chosen = [r[2] for r in rows if r[1] == options["category"].strip().lower()]
        print(money(sum(chosen, Decimal(0)) / len(chosen)) if chosen else "0.00")
    elif command == "max":
        if rows:
            best = rows[0]
            for row in rows[1:]:
                if row[2] > best[2] or (row[2] == best[2] and row[0] < best[0]):
                    best = row
            print("%s\t%s\t%s" % (best[0].isoformat(), best[1], money(best[2])))
    elif command == "categories":
        counts = {}
        for _, category, _ in rows:
            counts[category] = counts.get(category, 0) + 1
        for category in sorted(counts):
            print("%s\t%d" % (category, counts[category]))
    elif command == "refunds":
        chosen = [r[2] for r in rows if r[2] < 0]
        print("count=%d total=%s" % (len(chosen), money(sum(chosen, Decimal(0)))))


if __name__ == "__main__":
    main(sys.argv[1:])
