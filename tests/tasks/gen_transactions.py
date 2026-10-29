#!/usr/bin/env python3
"""Generate a messy transactions.csv for testing the programming task.

Usage: python3 gen_transactions.py OUTPUT.csv [--rows 40000] [--seed 1]

About 2% of rows are invalid (bad dates, bad amounts, empty categories, wrong
field counts). Categories use mixed capitalization and stray spaces, and some
descriptions are quoted and contain commas or escaped quotes.
"""
import argparse
import csv
import datetime
import random

CATEGORIES = ["groceries", "rent", "utilities", "dining", "transport", "entertainment",
              "health", "travel", "shopping", "education", "insurance", "gifts"]
WORDS = ["coffee", "market", "monthly", "online", "store", "ticket", "bill", "lunch", "dinner",
         "taxi", "bus pass", "pharmacy", "hotel", "flight", "book", "course", "premium", "present"]


def amount_text(rng):
    cents = rng.randint(50, 250000)
    if rng.random() < 0.06:
        cents = -rng.randint(50, 20000)          # refunds
    value = "%d.%02d" % (abs(cents) // 100, abs(cents) % 100)
    if cents < 0:
        value = "-" + value
    style = rng.random()
    if style < 0.05 and cents % 100 == 0:
        value = value.split(".")[0]              # whole number without decimals
    elif style < 0.08:
        value = " %s " % value                   # surrounding spaces
    return value


def category_text(rng):
    name = rng.choice(CATEGORIES)
    style = rng.random()
    if style < 0.15:
        name = name.upper()
    elif style < 0.3:
        name = name.capitalize()
    if rng.random() < 0.05:
        name = " %s " % name
    return name


def description(rng):
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3)))
    if rng.random() < 0.1:
        text += ", invoice %d" % rng.randint(100, 999)       # comma: must be quoted
    if rng.random() < 0.03:
        text += ' "special"'                                   # embedded quotes
    return text


def invalid_row(rng, date, desc, cat, amt):
    kind = rng.randrange(7)
    if kind == 0:
        return [date[:8] + "31" if date[5:7] in ("02", "04", "06", "09", "11") else "2026-02-30", desc, cat, amt]
    if kind == 1:
        return [date[8:10] + "/" + date[5:7] + "/" + date[:4], desc, cat, amt]   # wrong date format
    if kind == 2:
        return [date, desc, cat, rng.choice(["abc", "", "12.3.4", "N/A", "$-"])]  # bad amount
    if kind == 3:
        return [date, desc, rng.choice(["", "   "]), amt]                          # empty category
    if kind == 4:
        return [date, desc, cat]                                                   # too few fields
    if kind == 5:
        return [date, desc, cat, amt, "extra"]                                     # too many fields
    return ["", desc, cat, amt]                                                    # empty date


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--rows", type=int, default=40000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    start = datetime.date(2025, 1, 1)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "description", "category", "amount"])
        for _ in range(args.rows):
            date = (start + datetime.timedelta(days=rng.randint(0, 637))).isoformat()
            row = [date, description(rng), category_text(rng), amount_text(rng)]
            if rng.random() < 0.02:
                row = invalid_row(rng, *row)
            writer.writerow(row)


if __name__ == "__main__":
    main()
