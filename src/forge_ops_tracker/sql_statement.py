"""Finds the SQL behind a database error and reduces it to something safe to send: the names of
the stored procedures, tables and views it touched, and (only if configuration.
capture_sql_statement is on) the statement itself with every string and number replaced by "?".
Ported from gems/forge_ops_tracker's SqlStatement, which is itself ported from the server's own
SqlStatementMasker/SqlObjectExtractor: same rules everywhere, and the server applies them again on
arrival, so a difference here can only ever mean less is masked client-side, never that something
unmasked gets stored.

Deliberately a single pass over a few patterns, not a SQL parser. What mask() recognizes, the same
as the server:
  * 'single quoted' strings, with '' as an escaped quote, including one cut off by truncation (no
    closing quote: masked to the end of the statement, never left half-visible)
  * a backslash-escaped quote inside a string ('o\\'brien', PostgreSQL's E'o\\'brien', and every
    MySQL string). Where a backslash is just a character (a standard PostgreSQL string ending in
    one), the closing quote reads as escaped and masking runs on to the end: more than needed,
    never less.
  * a string's type prefix along with it (E'', X'', N'', B'', U&''), so X'DEADBEEF' is one "?"
  * $tag$ dollar-quoted strings (PostgreSQL function bodies and DO blocks)
  * bare numbers: integers, decimals, exponents (3e10, 1.5E-3), hex (0x1F) and binary (0b101), but
    never the digits inside an identifier (orders2, $1 placeholders, sp_v2)
"double quoted" and `backtick quoted` text is an identifier, not a value, so it's left alone,
except on MySQL and MariaDB, where "double quotes" are a string by default: pass system= for those
and they're masked too.
"""

import re

MASK = "?"
MAX_LENGTH = 4000
MAX_NAMES = 10
MAX_NAME_LENGTH = 200
MAX_CAUSE_DEPTH = 5

# db.system values where "double quotes" are a string, not a name.
DOUBLE_QUOTED_STRING_SYSTEMS = {"mysql", "mariadb"}

# The same four patterns as the server's SqlStatementMasker, in the same order. re.ASCII because
# Ruby's \w and \d are ASCII only, and the two must agree on every statement. The prefix only
# counts as one when it isn't the end of a word, but the quote always does: LIKE'%x%', with no
# space, is still a string. A backslash may also be the statement's very last character (a string
# cut off right after one), so that string is still masked rather than left unmatched.
_STRING = r"(?:(?<![\w$])(?:[EeXxNnBb]|[Uu]&))?'(?:[^'\\]|\\(?:.|\Z)|'')*(?:'|\Z)"
_DOUBLE_QUOTED_STRING = r'"(?:[^"\\]|\\(?:.|\Z)|"")*(?:"|\Z)'
_DOLLAR_QUOTED = r"(?P<tag>\$[A-Za-z_]*\$).*?(?:(?P=tag)|\Z)"
_NUMBER = r"(?<![\w$.])(?:0[xX][0-9a-fA-F]+|0[bB][01]+|(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)(?!\w)"

_LITERAL = re.compile(f"{_STRING}|{_DOLLAR_QUOTED}|{_NUMBER}", re.DOTALL | re.ASCII)
_LITERAL_WITH_DOUBLE_QUOTES = re.compile(f"{_STRING}|{_DOUBLE_QUOTED_STRING}|{_DOLLAR_QUOTED}|{_NUMBER}", re.DOTALL | re.ASCII)

_PART = r'(?:[\w$#@]+|"[^"]+"|\[[^\]]+\]|`[^`]+`)'
_NAME = rf"{_PART}(?:\.{_PART})*"
_OPERATIONS = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "CALL", "EXEC", "EXECUTE", "CREATE", "ALTER", "DROP", "TRUNCATE"}
_PROCEDURE_CALL = re.compile(rf"\b(?:CALL|EXEC(?:UTE)?|PERFORM)\s+(?!IMMEDIATE\b|FUNCTION\b|PROCEDURE\b)({_NAME})", re.IGNORECASE)
_RELATION = re.compile(rf"\b(FROM|JOIN|INTO|UPDATE|TABLE)\s+({_NAME})(\s*\()?", re.IGNORECASE)
_SELECT_FUNCTION = re.compile(rf"\A\s*SELECT\s+({_NAME})\s*\(", re.IGNORECASE)
_BUILTINS = {
    "count", "sum", "min", "max", "avg", "now", "coalesce", "nullif", "lower", "upper", "length", "concat",
    "cast", "date_trunc", "current_timestamp", "current_date", "row_number", "rank", "json_build_object",
    "json_agg", "array_agg",
}
_FROM_INSIDE_FUNCTION = re.compile(r"\b(?:EXTRACT|SUBSTRING|TRIM|OVERLAY)\s*\([^()]*\)", re.IGNORECASE)
_KEYWORDS_NOT_NAMES = {"select", "set", "values", "where", "lateral", "only", "unnest", "generate_series"}
_FULL_NAME = re.compile(rf"\A{_NAME}\Z")
_FROM_WORD = re.compile(r"\bFROM\b", re.IGNORECASE)


def find_in(exc):
    """The raw statement off the error itself or, for an app that wraps a database error in its
    own exception, off whatever it was raised from. SQLAlchemy's StatementError/DBAPIError expose
    it as .statement; nothing else in Python's database stack puts it on the exception."""
    depth = 0
    while exc is not None and depth < MAX_CAUSE_DEPTH:
        statement = getattr(exc, "statement", None)
        if isinstance(statement, str) and statement.strip():
            return statement
        exc = exc.__cause__ or exc.__context__
        depth += 1
    return None


def mask(statement, system=None):
    """system is the statement's db.system ("postgresql", "mysql", ...), when known; any case."""
    if not statement or not statement.strip():
        return None
    double_quoted_strings = bool(system) and str(system).lower() in DOUBLE_QUOTED_STRING_SYSTEMS
    pattern = _LITERAL_WITH_DOUBLE_QUOTES if double_quoted_strings else _LITERAL
    masked = pattern.sub(MASK, statement)
    return f"{masked[:MAX_LENGTH]}..." if len(masked) > MAX_LENGTH else masked


def mask_for_span(statement, system=None):
    """The masked statement a database span carries as "db.statement": mask() above, capped at
    exactly MAX_LENGTH characters. None for a missing or blank statement."""
    if not isinstance(statement, str):
        return None
    masked = mask(statement, system)
    return masked[:MAX_LENGTH] if masked is not None else None


def objects(masked):
    """Takes an already-masked statement (so a keyword inside a string value can't be mistaken
    for SQL). Returns None when nothing recognizable was found."""
    if not masked or not masked.strip():
        return None

    sql = _FROM_INSIDE_FUNCTION.sub(" ", masked)
    procedures = _PROCEDURE_CALL.findall(sql)
    relations = []

    for keyword, name, paren in _RELATION.findall(sql):
        if name.lower() in _KEYWORDS_NOT_NAMES:
            continue
        function_call = bool(paren) and keyword.upper() in ("FROM", "JOIN")
        (procedures if function_call else relations).append(name)

    match = _SELECT_FUNCTION.match(sql)
    if match and match.group(1).lower() not in _BUILTINS and not _FROM_WORD.search(sql):
        procedures.append(match.group(1))

    first_word = re.match(r"\A\s*(\w+)", sql)
    operation = first_word.group(1).upper() if first_word else ""
    result = {}
    if operation in _OPERATIONS:
        result["operation"] = operation
    result["procedures"] = _clean(procedures)
    result["relations"] = _clean(relations)
    if not result["procedures"] and not result["relations"] and "operation" not in result:
        return None
    return result


def _clean(names):
    cleaned = []
    for name in names:
        name = name.strip()[:MAX_NAME_LENGTH]
        if _FULL_NAME.match(name) and name not in cleaned:
            cleaned.append(name)
    return cleaned[:MAX_NAMES]
