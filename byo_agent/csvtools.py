"""CSV tools. All arithmetic is done here with exact decimals, not by the model."""
import ast
import csv
import io
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .files import relative, safe_path


def parse_number(value):
    """Parse a cell such as "1,200", "$19.99" or "15%" into a Decimal, or None if not numeric."""
    if value is None:
        return None
    text = str(value).strip()
    for symbol in ("$", "€", "£", "¥", ",", "%", " "):
        text = text.replace(symbol, "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def format_number(number):
    """Format a Decimal without trailing zeros, e.g. 150.00 -> 150, 2.50 -> 2.5."""
    if number == number.to_integral_value():
        return str(int(number))
    rounded = number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = format(rounded.normalize(), "f")
    two_places = format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    if "." in text and len(text.split(".")[1]) > 2:
        text += " (%s rounded to 2 decimal places)" % two_places
    return text


def values_match(cell, target):
    """Compare a cell with a target value, ignoring case and surrounding spaces, and numerically."""
    a, b = str(cell).strip(), str(target).strip()
    if a.casefold() == b.casefold():
        return True
    x, y = parse_number(a), parse_number(b)
    return x is not None and y is not None and x == y


def find_column(header, name):
    """Return the index of a column, matching names loosely (case, spaces, underscores)."""
    wanted = str(name).strip()
    for i, column in enumerate(header):
        if column == wanted:
            return i
    for i, column in enumerate(header):
        if column.strip().casefold() == wanted.casefold():
            return i
    simplify = lambda s: re.sub(r"[^0-9a-z]", "", s.casefold())
    for i, column in enumerate(header):
        if simplify(column) == simplify(wanted):
            return i
    raise ValueError("column %r not found. Available columns: %s" % (name, ", ".join(header)))


def load_csv(path):
    """Read a CSV file and return (header, rows, format) where format keeps its delimiter, line ending and BOM."""
    full = safe_path(path)
    if not os.path.isfile(full):
        raise ValueError("file not found: %s" % path)
    with open(full, "rb") as f:
        raw = f.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    first_line = text.split("\n", 1)[0]
    delimiter = ","
    if "," not in first_line:
        for candidate in ("\t", ";", "|"):
            if candidate in first_line:
                delimiter = candidate
                break
    rows = [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter) if row]
    if not rows:
        raise ValueError("%s is empty." % path)
    fmt = {"delimiter": delimiter, "newline": "\r\n" if "\r\n" in text else "\n", "bom": bom}
    return rows[0], rows[1:], fmt


def save_csv(path, header, rows, fmt):
    """Write a CSV file in the same format it was read in, creating parent directories as needed."""
    full = safe_path(path)
    if os.path.isdir(full):
        raise ValueError("%s is a directory." % path)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=fmt["delimiter"], lineterminator=fmt["newline"])
    writer.writerow(header)
    writer.writerows(rows)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8-sig" if fmt["bom"] else "utf-8", newline="") as f:
        f.write(buffer.getvalue())
    return relative(full)


def column_values_hint(header, rows, index, limit=15):
    values = []
    for row in rows:
        if index < len(row) and row[index] not in values:
            values.append(row[index])
    return ", ".join(values[:limit]) + (" ..." if len(values) > limit else "")


def csv_column_stats(path, column):
    """Return count, total, average, minimum and maximum of a numeric CSV column."""
    header, rows, _ = load_csv(path)
    index = find_column(header, column)
    numbers, skipped = [], 0
    for row in rows:
        number = parse_number(row[index]) if index < len(row) else None
        if number is None:
            skipped += 1
        else:
            numbers.append(number)
    if not numbers:
        return "ERROR: column %r has no numeric values." % header[index]
    total = sum(numbers, Decimal(0))
    result = ("Column %r in %s: count=%d, total=%s, average=%s, minimum=%s, maximum=%s."
              % (header[index], path, len(numbers), format_number(total),
                 format_number(total / len(numbers)), format_number(min(numbers)),
                 format_number(max(numbers))))
    if skipped:
        result += " %d non-numeric or empty cell(s) were skipped." % skipped
    return result


def csv_row_sum(path, match_column, match_value, columns):
    """Add the numeric values of the given columns in the row(s) where match_column equals match_value."""
    header, rows, _ = load_csv(path)
    match_index = find_column(header, match_column)
    if isinstance(columns, str):
        columns = [c for c in re.split(r"[,;]", columns) if c.strip()]
    indexes = [find_column(header, c) for c in columns]
    matched = [row for row in rows if match_index < len(row) and values_match(row[match_index], match_value)]
    if not matched:
        return ("ERROR: no row where %r is %r. Values in that column: %s"
                % (header[match_index], match_value, column_values_hint(header, rows, match_index)))
    lines = []
    for row in matched:
        parts, total = [], Decimal(0)
        for i in indexes:
            number = parse_number(row[i]) if i < len(row) else None
            if number is None:
                return "ERROR: value %r in column %r is not numeric." % (row[i] if i < len(row) else "", header[i])
            parts.append("%s=%s" % (header[i], format_number(number)))
            total += number
        lines.append("Row where %s=%s: %s, sum=%s."
                     % (header[match_index], row[match_index], ", ".join(parts), format_number(total)))
    return " ".join(lines)


def csv_update_value(path, match_column, match_value, column, new_value, output_path=None):
    """Set `column` to `new_value` in rows where match_column equals match_value, then save."""
    header, rows, fmt = load_csv(path)
    match_index = find_column(header, match_column)
    target_index = find_column(header, column)
    new_value = "" if new_value is None else str(new_value)
    updated = 0
    for row in rows:
        if match_index < len(row) and values_match(row[match_index], match_value):
            while len(row) <= target_index:
                row.append("")
            row[target_index] = new_value
            updated += 1
    if not updated:
        return ("ERROR: no row where %r is %r. Nothing was saved. Values in that column: %s"
                % (header[match_index], match_value, column_values_hint(header, rows, match_index)))
    saved = save_csv(output_path or path, header, rows, fmt)
    return ("Updated %d row(s): set %r to %r where %r is %r. Saved %d data rows to %s."
            % (updated, header[target_index], new_value, header[match_index], match_value, len(rows), saved))


def csv_add_to_value(path, match_column, match_value, column, amount, output_path=None):
    """Increase the numeric value of `column` by `amount` in rows where match_column equals match_value, then save."""
    header, rows, fmt = load_csv(path)
    match_index = find_column(header, match_column)
    target_index = find_column(header, column)
    delta = parse_number(amount)
    if delta is None:
        return "ERROR: amount %r is not a number." % (amount,)
    changes = []
    for row in rows:
        if match_index < len(row) and values_match(row[match_index], match_value):
            current = parse_number(row[target_index]) if target_index < len(row) else Decimal(0)
            if current is None:
                return "ERROR: value %r in column %r is not numeric. Nothing was saved." % (row[target_index], header[target_index])
            while len(row) <= target_index:
                row.append("")
            row[target_index] = format(current + delta, "f")
            changes.append("%s -> %s" % (format_number(current), row[target_index]))
    if not changes:
        return ("ERROR: no row where %r is %r. Nothing was saved. Values in that column: %s"
                % (header[match_index], match_value, column_values_hint(header, rows, match_index)))
    saved = save_csv(output_path or path, header, rows, fmt)
    return ("Added %s to %r where %r is %r (%s). Saved %d data rows to %s."
            % (format_number(delta), header[target_index], header[match_index], match_value,
               ", ".join(changes), len(rows), saved))


CONDITIONS = {
    "equals": "equals", "equal": "equals", "eq": "equals", "is": "equals", "=": "equals", "==": "equals",
    "not_equals": "not_equals", "not equal": "not_equals", "ne": "not_equals", "!=": "not_equals",
    "is_not": "not_equals",
    "greater_than": "greater_than", "gt": "greater_than", ">": "greater_than",
    "less_than": "less_than", "lt": "less_than", "<": "less_than",
    "greater_or_equal": "greater_or_equal", "gte": "greater_or_equal", ">=": "greater_or_equal",
    "less_or_equal": "less_or_equal", "lte": "less_or_equal", "<=": "less_or_equal",
    "contains": "contains",
}


def row_matches(cell, condition, value):
    if condition == "equals":
        return values_match(cell, value)
    if condition == "not_equals":
        return not values_match(cell, value)
    if condition == "contains":
        return str(value).strip().casefold() in str(cell).casefold()
    x, y = parse_number(cell), parse_number(value)
    if x is None or y is None:
        return False
    return {"greater_than": x > y, "less_than": x < y,
            "greater_or_equal": x >= y, "less_or_equal": x <= y}[condition]


def csv_delete_rows(path, column, condition, value, output_path=None):
    """Remove rows whose `column` meets the condition, then save the remaining rows."""
    header, rows, fmt = load_csv(path)
    index = find_column(header, column)
    key = str(condition).strip().lower()
    condition_name = CONDITIONS.get(key) or CONDITIONS.get(key.replace(" ", "_"))
    if condition_name is None:
        return "ERROR: unknown condition %r. Use one of: %s" % (
            condition, "equals, not_equals, greater_than, less_than, greater_or_equal, less_or_equal, contains")
    kept = [row for row in rows if not (index < len(row) and row_matches(row[index], condition_name, value))]
    removed = len(rows) - len(kept)
    saved = save_csv(output_path or path, header, kept, fmt)
    result = ("Removed %d row(s) where %r %s %r. Saved %d remaining data rows to %s."
              % (removed, header[index], condition_name, value, len(kept), saved))
    if not removed:
        result += " Note: no rows matched. Values in that column: %s" % column_values_hint(header, rows, index)
    return result


def calculate(expression):
    """Evaluate a basic arithmetic expression exactly (+ - * / and parentheses)."""
    operators = {
        ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    }

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = evaluate(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        raise ValueError("unsupported expression")

    cleaned = str(expression).replace(",", "").replace("$", "").replace("x", "*").replace("×", "*")
    return "%s = %s" % (expression, format_number(evaluate(ast.parse(cleaned, mode="eval"))))
