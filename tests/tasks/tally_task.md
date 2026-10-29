# Task: implement `tally.py`

Write a command-line tool `tally.py` (Python 3, standard library only) in the current directory.
It summarises spending stored in a CSV file. A sample data file, `transactions.csv`, is provided
(it is large: about 40,000 rows).

## Invocation

```
python3 tally.py <command> [arguments] [--file PATH] [--json]
```

* `--file PATH` selects the CSV file. Default: `transactions.csv` in the current directory.
* Options may appear anywhere after the command name.

## CSV format

The first line is a header: `date,description,category,amount`. Every following line is one transaction.
Fields follow normal CSV quoting rules (a field may be wrapped in double quotes and contain commas).
`amount` is a decimal number of dollars (it may be negative, e.g. a refund).

## Requirements

Every requirement below is checked by an automated test. Output must match exactly
(no extra lines, no extra spaces). All money values are printed with exactly two decimals
and no thousands separator, e.g. `1234.50`, `-3.00`.

1. **`count`** — `python3 tally.py count` prints the number of valid transactions, e.g. `39876`.
2. **`total`** — `python3 tally.py total` prints the sum of all amounts, e.g. `1834512.07`.
3. **`total --category NAME`** (an option of the `total` command, not a separate command) — `python3 tally.py total --category NAME` prints the sum of amounts of
   transactions whose category equals NAME. The comparison is case-insensitive.
4. **`top N`** — `python3 tally.py top N` prints the N categories with the largest total amount, one per line,
   as `CATEGORY<TAB>AMOUNT`. Sort by amount descending; if two amounts are equal, sort those categories
   alphabetically ascending. If there are fewer than N categories, print all of them.
5. **category case** — categories are case-insensitive everywhere: `Food`, `FOOD` and `food` are the
   same category. Whenever a category name is printed, it is printed in lowercase.
6. **`range --from D1 --to D2`** — `python3 tally.py range --from YYYY-MM-DD --to YYYY-MM-DD` considers transactions whose
   date is between the two dates, both ends inclusive, and prints one line `count=K total=X.XX`.
7. **invalid rows** — a row is invalid if it does not have exactly 4 fields, if its date is not a real calendar
   date written as `YYYY-MM-DD`, if its amount is not a decimal number, or if its category is empty. Spaces
   around a field are ignored. Invalid rows are ignored by every command.
8. **`months`** — `python3 tally.py months` prints one line per calendar month that has transactions, in
   ascending order, as `YYYY-MM<TAB>AMOUNT`, where AMOUNT is that month's total.
9. **`avg --category NAME`** — prints the average amount of the category's transactions, rounded to two decimals
   (round half up). If the category has no transactions, print `0.00`.
10. **`refunds`** — prints `count=K total=X.XX` for transactions with a negative amount.
11. **`--json`** — with `--json`, `count` prints `{"count": K}`, `total` prints `{"total": "X.XX"}`, and `range`
    prints `{"count": K, "total": "X.XX"}` (money as strings with two decimals).
12. **errors** — for an unknown command, or a missing or invalid argument (for example `top abc`), print one line
    starting with `error:` to standard error and exit with status 2. If the data file does not exist, print one
    line starting with `error:` to standard error and exit with status 1. Nothing is printed to standard output
    in these cases.

13. **`max`** — prints the single largest transaction as `YYYY-MM-DD<TAB>CATEGORY<TAB>AMOUNT`. If several
    transactions share the largest amount, print the one with the earliest date (and among those, the first one
    in the file). If there are no valid transactions, print nothing.
14. **`categories`** — prints every category with its number of transactions, one per line, as
    `CATEGORY<TAB>COUNT`, sorted alphabetically by category.
15. **`top N --json`** — with `--json`, `top` prints a JSON list such as
    `[{"category": "rent", "total": "1200.00"}, {"category": "food", "total": "35.50"}]`, in the same order as
    the plain output.
16. **argument checks** — `N` for `top` must be a positive integer (so `top 0` and `top -3` are errors). For
    `range`, both dates must be real calendar dates, both options are required, and `--from` must not be later
    than `--to`. All of these are errors as described in requirement 12 (exit status 2).
17. **`--category` filter** — `count`, `refunds` and `months` also accept `--category NAME`. It keeps only the
    transactions of that category (case-insensitive) before computing the usual output.
18. **no data** — if the file has a header but no valid transactions, `count` prints `0`, `total` prints `0.00`,
    `refunds` prints `count=0 total=0.00`, and `top`, `months`, `categories` and `max` print nothing. These are not
    errors (exit status 0).

When you are done, `tally.py` must be in the current directory.

Scoring: each of the 18 requirements is checked by an automated test and is worth 1 point.
