"""File tools. Every path is resolved inside the workspace."""
import difflib
import json
import os
import re
import shutil
import warnings

from . import config


def safe_path(path):
    """Resolve a path inside the workspace. Rejects any path that would escape it."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Empty path.")
    path = path.strip()
    full = os.path.realpath(os.path.join(config.WORKSPACE, path))
    # Treat "/abc/foo.txt" as "abc/foo.txt" when the model writes a leading slash.
    if os.path.isabs(path) and not is_inside_workspace(full):
        full = os.path.realpath(os.path.join(config.WORKSPACE, path.lstrip("/")))
    if not is_inside_workspace(full):
        raise ValueError("Path %r is outside the workspace." % path)
    return full


def is_inside_workspace(full):
    return os.path.commonpath([full, config.WORKSPACE]) == config.WORKSPACE


def relative(full):
    return os.path.relpath(full, config.WORKSPACE)


def describe_size(full):
    """'(12 lines, 0.3 KB)' for a file."""
    size = os.path.getsize(full)
    if size > 50 * 1024 * 1024:
        return "(%.1f MB)" % (size / 1048576.0)
    lines, last = 0, b"\n"
    with open(full, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if size and last != b"\n":
        lines += 1
    return "(%d lines, %.1f KB)" % (lines, size / 1024.0)


def list_files(path="."):
    """Return all files under a directory, one relative path per line with its size."""
    root = safe_path(path)
    if not os.path.isdir(root):
        return "ERROR: not a directory: %s" % path
    files = []
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(folder, name)
            try:
                files.append("%s  %s" % (relative(full), describe_size(full)))
            except OSError:
                files.append(relative(full))
            if len(files) >= 300:
                return "\n".join(files) + "\n(truncated)"
    return "\n".join(files) if files else "(no files)"


def read_text(full, keep_newlines=False):
    with open(full, "r", encoding="utf-8", errors="replace", newline="" if keep_newlines else None) as f:
        return f.read()


def read_file(path, start_line=None, end_line=None):
    """Return a file's text. Small files are returned whole; for large files, a range of lines."""
    full = safe_path(path)
    if not os.path.isfile(full):
        return "ERROR: file not found: %s" % path
    text = read_text(full)
    code = full.endswith((".py", ".sh", ".js", ".java", ".c", ".cpp", ".go", ".rb"))
    if start_line is None and end_line is None and len(text) <= config.FULL_READ_LIMIT and not code:
        return text
    lines = text.splitlines(True)
    total = len(lines)
    try:
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line not in (None, "") else total
    except (TypeError, ValueError):
        return "ERROR: start_line and end_line must be line numbers."
    if start > total:
        return "ERROR: %s has only %d lines." % (relative(full), total)
    end = min(max(end, start), total)
    chunk, size, last = [], 0, start - 1
    for number in range(start, end + 1):
        line = lines[number - 1].rstrip("\n")
        if len(line) > 2000:
            line = line[:2000] + "...(line truncated)"
        line = "%6d| %s\n" % (number, line)
        if size + len(line) > config.PART_READ_LIMIT and chunk:
            break
        chunk.append(line)
        size += len(line)
        last = number
    header = ("[%s has %d lines. Showing lines %d-%d, each prefixed with its line number and '| ' "
              "(the prefix is not part of the file)." % (relative(full), total, start, last))
    if last < total:
        header += " Use start_line/end_line to read more, or search_files to find specific lines."
    return header + "]\n" + "".join(chunk)


def search_files(pattern, path=".", regex=False, ignore_case=True, context=0, max_results=40):
    """Find lines matching a text or regular expression in a file or in every file under a folder."""
    root = safe_path(path)
    if not os.path.exists(root):
        return "ERROR: not found: %s" % path
    flags = re.IGNORECASE if ignore_case not in (False, "false", "False") else 0
    try:
        matcher = re.compile(str(pattern) if regex in (True, "true", "True") else re.escape(str(pattern)), flags)
    except re.error as e:
        return "ERROR: invalid regular expression: %s" % e
    try:
        context = max(0, min(int(context or 0), 3))
        # The model often asks for very few results and then misses the line it needs.
        max_results = max(15, min(int(max_results or 40), 200))
    except (TypeError, ValueError):
        context, max_results = 0, 40
    targets = []
    if os.path.isdir(root):
        for folder, dirs, names in os.walk(root):
            dirs.sort()
            targets.extend(os.path.join(folder, n) for n in sorted(names))
    else:
        targets = [root]

    out, found, important = [], 0, []
    for full in targets:
        try:
            lines = read_text(full).splitlines()
        except OSError:
            continue
        matches = []
        for index, line in enumerate(lines):
            if matcher.search(line):
                found += 1
                if found <= max_results:
                    matches.append(index)
                elif len(important) < 10 and IMPORTANT.search(line):
                    text = line if len(line) <= 500 else line[:500] + "...(truncated)"
                    important.append("%s%s:%d: %s" % ("MATCH   " if context else "", relative(full), index + 1, text))
        shown = -1
        match_set = set(matches)
        for index in matches:
            first = max(index - context, shown + 1)
            last = min(index + context, len(lines) - 1)
            if first > last:
                continue
            if context and out and (first > shown + 1 or index == matches[0]):
                out.append("--")
            for i in range(first, last + 1):
                text = lines[i] if len(lines[i]) <= 500 else lines[i][:500] + "...(truncated)"
                if context:
                    out.append("%s %s:%d: %s" % ("MATCH  " if i in match_set else "context", relative(full), i + 1, text))
                else:
                    out.append("%s:%d: %s" % (relative(full), i + 1, text))
                shown = i
    if not found:
        return "No matches for %r." % pattern
    result = "\n".join(out)
    if context:
        result = ("(Lines starting with MATCH contain the pattern. Lines starting with context are only neighbours "
                  "and are usually about other requests.)\n" + result)
    if found > max_results:
        result += ("\n... %d more matches not shown. Use a more specific pattern (for example add a word with "
                   "regex true, such as 'inv-1.*ERROR')." % (found - max_results))
        if important:
            result += "\nAmong the matches not shown, these are errors or warnings:\n" + "\n".join(important)
    return result


# Log lines worth showing even when there are too many matches to show them all.
IMPORTANT = re.compile(r"\b(ERROR|FATAL|CRITICAL|WARN|WARNING|SEVERE)\b|Exception|Traceback|stalled|deadlock|"
                       r"timed out|timeout", re.I)


def normalize_content(content):
    # The model sometimes sends lines as a list instead of one string.
    if isinstance(content, list):
        return "\n".join(str(line) for line in content)
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def numbered_excerpt(content, line_number, radius=3):
    lines = content.split("\n")
    first, last = max(1, line_number - radius), min(len(lines), line_number + radius)
    return "\n".join("%6d| %s" % (n, lines[n - 1][:300]) for n in range(first, last + 1))


def check_file(full, content):
    """Extra feedback after writing: syntax errors in Python, validity of JSON, word counts of text."""
    notes = ""
    if full.endswith(".py"):
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                compile(content, full, "exec")
            notes += " Python syntax OK."
            for w in caught[:3]:
                notes += " Python warning at line %s: %s." % (getattr(w, "lineno", "?"), w.message)
        except SyntaxError as e:
            notes += " WARNING: Python syntax error at line %s: %s. Lines around it:\n%s" % (
                e.lineno, e.msg, numbered_excerpt(content, e.lineno or 1))
            if re.search(r"""\bf["']""", e.text or ""):
                notes += ("\nHint: this is usually caused by quotes or braces inside an f-string. Build the "
                          "value first and print it without an f-string, e.g. print(json.dumps({\"total\": value})) "
                          "for JSON or print(\"count=%d total=%s\" % (count, total)) for text.")
    elif full.endswith(".json"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                notes += " Valid JSON object with %d keys: %s." % (len(data), ", ".join(data))
            else:
                notes += " Valid JSON (not an object)."
        except ValueError as e:
            notes += " WARNING: not valid JSON: %s." % e
    elif full.endswith((".txt", ".md")):
        # The model is unreliable at counting words, so report the count and ask it to shorten if needed.
        limit = word_limit(config.PROMPT)
        if limit:
            count = len(content.split())
            notes += " The file has %d words." % count
            if count >= limit:
                notes += (" This is NOT under the requested limit of %d words. "
                          "Rewrite the file shorter." % limit)
    return notes


def python_error(full, content):
    """Return the SyntaxError for Python content, or None if it compiles (or is not a .py file)."""
    if not full.endswith(".py"):
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(content, full, "exec")
        return None
    except SyntaxError as e:
        return e
    except (ValueError, TypeError):
        return None


def shift_lines(lines, delta, skip_first=False):
    """Indent (delta > 0) or dedent (delta < 0) every non-blank line. None if a line cannot be dedented."""
    out = []
    for n, line in enumerate(lines):
        if not line.strip() or (skip_first and n == 0):
            out.append(line)
        elif delta >= 0:
            out.append(" " * delta + line)
        elif len(line) - len(line.lstrip(" ")) >= -delta:
            out.append(line[-delta:])
        else:
            return None
    return out


def indent_variants(new_lines, target_indent):
    """Other indentations of a replacement block, tried when the given one breaks a Python file.

    Covers the model's usual slips: only the first line indented differently from the replaced line,
    or the whole block lined up wrongly. Both variants line the block up with the replaced line.
    """
    variants = []
    body = [l for l in new_lines if l.strip()]
    if not body:
        return variants
    first = body[0]
    first_indent = len(first) - len(first.lstrip(" "))
    index = new_lines.index(first)
    if first_indent != target_indent:
        fixed = list(new_lines)
        fixed[index] = " " * target_indent + first.lstrip(" ")
        variants.append(fixed)
        variants.append(shift_lines(new_lines, target_indent - first_indent))
    return [v for v in variants if v is not None and v != new_lines]


def save_edit(full, before, after, done, variants=()):
    """Write an edited file, unless the edit would break a Python file that compiled before.

    If the edit breaks it but one of `variants` (the same edit with other indentation) compiles,
    that variant is saved instead. Refusing broken edits keeps a working program working;
    repeated broken one-line patches otherwise pile up and damage the file.
    """
    error = python_error(full, after)
    if error is not None and python_error(full, before) is None:
        for variant in variants:
            if python_error(full, variant) is None:
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(variant)
                return done + " The indentation of the new text was adjusted to fit the surrounding code." + \
                    check_file(full, variant)
        return ("ERROR: the change was NOT applied, because it would make %s invalid Python (line %s: %s). "
                "With the change, the lines around it would have been:\n%s\nThe file is unchanged. Check that "
                "the new text is complete (for example every try: has its except:) and indented like the "
                "lines around it, then try again, or rewrite the whole file with write_file."
                % (relative(full), error.lineno, error.msg, numbered_excerpt(after, error.lineno or 1)))
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(after)
    return done + check_file(full, after)


def write_file(path, content):
    """Create or overwrite a file, creating parent directories as needed."""
    full = safe_path(path)
    content = normalize_content(content)
    if os.path.isdir(full):
        return "ERROR: %s is a directory." % path
    os.makedirs(os.path.dirname(full), exist_ok=True)
    unchanged = os.path.isfile(full) and read_text(full, keep_newlines=True) == content
    # newline="" writes the content exactly as given, with no line-ending conversion.
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    note = (" The file already had exactly this content, so nothing changed: a fix you meant to make is not in it."
            if unchanged else "")
    return "Wrote %d characters to %s.%s%s" % (len(content), relative(full), note, check_file(full, content))


def append_file(path, content):
    """Add text to the end of a file (creating it if needed). Used to write long files in parts.

    If the new text starts the same way as the file, the model is resending the whole file, so the
    file is replaced instead of getting a second copy appended.
    """
    full = safe_path(path)
    content = normalize_content(content)
    if os.path.isdir(full):
        return "ERROR: %s is a directory." % path
    if os.path.isfile(full):
        head = [l for l in read_text(full).split("\n") if l.strip()][:3]
        new_head = [l for l in content.split("\n") if l.strip()][:3]
        if len(head) == 3 and head == new_head:
            return write_file(path, content) + (" (The text started like the whole file, so it replaced the file "
                                                "instead of being appended to it.)")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a", encoding="utf-8", newline="") as f:
        f.write(content)
    text = read_text(full)
    return "Appended %d characters to %s (now %d lines).%s" % (
        len(content), relative(full), len(text.splitlines()), check_file(full, text))


LINE_PREFIX = re.compile(r"^\s*\d+\| ?")


def strip_line_numbers(text):
    """Remove 'NNN| ' prefixes copied from read_file output, if every non-empty line has one."""
    lines = text.split("\n")
    if lines and all(LINE_PREFIX.match(l) for l in lines if l.strip()) and any(l.strip() for l in lines):
        return "\n".join(LINE_PREFIX.sub("", l, count=1) for l in lines)
    return text


def find_loose(lines, old_lines):
    """Find where old_lines occur in lines, ignoring indentation and trailing spaces. Returns start indexes."""
    want = [l.strip() for l in old_lines]
    while want and not want[0]:
        want.pop(0)
    while want and not want[-1]:
        want.pop()
    if not want:
        return [], 0
    found = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + j].strip() == want[j] for j in range(len(want))):
            found.append(i)
    return found, len(want)


def reindent(new_lines, file_line, old_first):
    """Shift new_lines so their indentation lines up with the matched file line."""
    file_indent = len(file_line) - len(file_line.lstrip())
    old_indent = len(old_first) - len(old_first.lstrip())
    shift = file_indent - old_indent
    if shift == 0:
        return new_lines
    out = []
    for line in new_lines:
        if shift > 0:
            out.append(" " * shift + line if line.strip() else line)
        else:
            cut = min(-shift, len(line) - len(line.lstrip()))
            out.append(line[cut:])
    return out


def edit_file(path, old_text, new_text, replace_all=False):
    """Replace a piece of text in a file. It must occur exactly once unless replace_all is true.

    If the exact text is not found, a match that ignores indentation and line-number prefixes is tried.
    """
    full = safe_path(path)
    if not os.path.isfile(full):
        return "ERROR: file not found: %s" % path
    old_text = strip_line_numbers(normalize_content(old_text))
    new_text = strip_line_numbers(normalize_content(new_text))
    if not old_text.strip():
        return "ERROR: old_text is empty."
    text = read_text(full, keep_newlines=True)
    count = text.count(old_text)
    if count == 1 or (count > 1 and replace_all in (True, "true", "True")):
        variants = []
        if count == 1 and full.endswith(".py"):
            # Work on the whole lines around the match, so the new text can be re-indented as a block.
            at = text.index(old_text)
            line_start = text.rfind("\n", 0, at) + 1
            line_end = text.find("\n", at + len(old_text))
            line_end = len(text) if line_end < 0 else line_end
            region = text[line_start:line_end]
            indent = len(region) - len(region.lstrip(" "))
            for variant in indent_variants(region.replace(old_text, new_text, 1).split("\n"), indent):
                variants.append(text[:line_start] + "\n".join(variant) + text[line_end:])
        return save_edit(full, text, text.replace(old_text, new_text),
                         "Replaced %d occurrence(s) in %s." % (count, relative(full)), variants)
    if count > 1:
        return "ERROR: old_text occurs %d times. Include more surrounding lines so it is unique." % count

    lines = text.split("\n")
    old_lines = old_text.split("\n")
    starts, length = find_loose(lines, old_lines)
    if len(starts) == 1:
        i = starts[0]
        first_old = next(l for l in old_lines if l.strip())
        new_lines = reindent(new_text.split("\n"), lines[i], first_old)
        while new_lines and not new_lines[-1].strip() and len(new_lines) > 1:
            new_lines.pop()
        target = len(lines[i]) - len(lines[i].lstrip(" "))
        variants = ["\n".join(lines[:i] + v + lines[i + length:])
                    for v in indent_variants(new_lines, target)] if full.endswith(".py") else []
        lines[i:i + length] = new_lines
        return save_edit(full, text, "\n".join(lines), "Replaced lines %d-%d in %s (matched ignoring indentation)."
                         % (i + 1, i + length, relative(full)), variants)
    if len(starts) > 1:
        return "ERROR: old_text occurs %d times. Include more surrounding lines so it is unique." % len(starts)
    hint = ""
    probe = next((l.strip() for l in old_lines if l.strip()), "")
    close = difflib.get_close_matches(probe, [l.strip() for l in lines], n=1, cutoff=0.5)
    if close:
        number = [l.strip() for l in lines].index(close[0]) + 1
        hint = " The most similar line is line %d: %s" % (number, lines[number - 1].strip()[:200])
    return ("ERROR: old_text was not found in %s.%s Use replace_lines with line numbers instead, "
            "or rewrite the whole file with write_file." % (relative(full), hint))


def replace_lines(path, start_line, end_line, new_text):
    """Replace lines start_line..end_line (1-based, inclusive) with new_text. Empty new_text deletes them."""
    full = safe_path(path)
    if not os.path.isfile(full):
        return "ERROR: file not found: %s" % path
    try:
        start, end = int(start_line), int(end_line)
    except (TypeError, ValueError):
        return "ERROR: start_line and end_line must be line numbers."
    text = read_text(full, keep_newlines=True)
    lines = text.split("\n")
    trailing = text.endswith("\n")
    if trailing:
        lines.pop()
    if not 1 <= start <= end <= len(lines):
        return "ERROR: lines must be between 1 and %d, with start_line <= end_line." % len(lines)
    new_text = strip_line_numbers(normalize_content(new_text))
    new_lines = new_text.split("\n") if new_text != "" else []
    if new_lines and new_lines[-1] == "":
        new_lines.pop()
    # The model often sends replacement code without its indentation; line it up with the old line.
    old_first = lines[start - 1]
    body = [l for l in new_lines if l.strip()]
    old_indent = len(old_first) - len(old_first.lstrip())
    if body and old_first.strip() and min(len(l) - len(l.lstrip()) for l in body) == 0 and old_indent > 0:
        rest = body[1:]
        if rest and len(body[0]) == len(body[0].lstrip()) and \
                all(len(l) - len(l.lstrip()) > old_indent for l in rest):
            # Only the first line lost its indentation (copied from after a line-number prefix).
            index = new_lines.index(body[0])
            new_lines[index] = old_first[:old_indent] + body[0]
        else:
            new_lines = reindent(new_lines, old_first, body[0])
    target = len(old_first) - len(old_first.lstrip(" "))
    variants = [("\n".join(lines[:start - 1] + v + lines[end:]) + ("\n" if trailing else ""))
                for v in indent_variants(new_lines, target)] if full.endswith(".py") else []
    old_block = lines[start - 1:end]
    lines[start - 1:end] = new_lines
    result = save_edit(full, text, "\n".join(lines) + ("\n" if trailing else ""),
                       "Replaced lines %d-%d of %s with %d line(s)." % (start, end, relative(full), len(new_lines)),
                       variants)
    if result.startswith("ERROR"):
        return result
    return result + edit_feedback(full, old_block, new_lines, start, end)


STRUCTURE = re.compile(r"^\s*(def |class |elif |if |else\b|for |while |try\b|except\b|with |return\b)")


def edit_feedback(full, old_block, new_lines, start, end):
    """After a line edit of a code file: warn about removed code lines, and show the edited lines with
    their new numbers, because every later line number has moved."""
    if not full.endswith((".py", ".sh", ".js")):
        return ""
    kept = {l.strip() for l in new_lines}
    removed = [l.strip() for l in old_block if STRUCTURE.match(l) and l.strip() not in kept]
    note = ""
    if removed:
        note += ("\nWARNING: these lines were removed: %s. If you did not mean to remove them, put them back."
                 % " | ".join(removed[:5]))
    text = read_text(full)
    shift = len(new_lines) - (end - start + 1)
    first = max(1, start - 2)
    last = start + len(new_lines) + 1
    note += "\nThe edited part now reads:\n%s" % "\n".join(
        "%6d| %s" % (n, l) for n, l in enumerate(text.split("\n")[first - 1:last], first))
    if shift:
        note += "\nLines after line %d moved by %+d. Read the file again before the next edit by line number." % (
            end, shift)
    return note


def delete_file(path):
    """Delete a single regular file. Directories are never deleted."""
    full = safe_path(path)
    if full == config.WORKSPACE:
        return "ERROR: refusing to delete the workspace."
    if os.path.isdir(full):
        return "ERROR: %s is a directory. Only files can be deleted." % path
    if not os.path.isfile(full):
        return "ERROR: file not found: %s" % path
    os.remove(full)
    return "Deleted %s." % relative(full)


def copy_file(source, destination):
    """Copy a file byte for byte, creating parent directories as needed."""
    src = safe_path(source)
    dst = safe_path(destination)
    if not os.path.isfile(src):
        return "ERROR: file not found: %s" % source
    if os.path.isdir(dst):
        return "ERROR: %s is a directory." % destination
    if src == dst:
        return "ERROR: source and destination are the same file."
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return "Copied %s to %s." % (relative(src), relative(dst))


WORD_LIMIT_PATTERNS = [
    r"(?:less than|fewer than|under|below|at most|no more than|no longer than|"
    r"not exceeding|not exceed|maximum of|max(?:imum)?\.?|up to|within)\s+(\d+)\s+words",
    r"(\d+)\s+words?\s+or\s+(?:less|fewer)",
    r"(\d+)[- ]word\b",
]


def word_limit(text):
    """Return the word limit stated in the prompt (e.g. 'less than 100 words'), or None."""
    for pattern in WORD_LIMIT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None
