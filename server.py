"""A very simple MCP server (stdio).

Provides a single `run_sql` tool that takes an SQL query from the user,
prints it to the screen (to stderr, so as not to interfere with the MCP
stdio protocol), writes it to a log file, and returns it back to the client.
"""

import difflib
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import sqlglot
from sqlglot import expressions as exp
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlglot.optimizer.qualify import qualify

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    psycopg2 = None

mcp = FastMCP(
    "fugly-sql",
    instructions=(
        "The run_sql tool is an EXECUTOR; it does not fix syntax. "
        "Fixing is the MODEL's responsibility: before calling run_sql the "
        "model must turn the passed SQL into a syntactically valid form, "
        "preserving the original meaning. Typical errors must be fixed: "
        "missing keywords (VALUES), a missing column list in INSERT, "
        "unescaped reserved words such as table, extra/missing commas. "
        "Example: "
        "'insert into table2 1,1,1' -> "
        "INSERT INTO table2 (table1_id, info) VALUES (1, 1, 1). "
        "Typos in table/column names may be left — the server itself will "
        "resolve them against the schema, but the syntax must already arrive "
        "valid."
    ),
)


# PostgreSQL connection parameters
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "postgres",
    "user": "postgres",
    "password": "postgres",
}


def fetch_schema_from_postgres():
    """Load the database schema from PostgreSQL.

    Returns a dict of the form {table: {column: type}}.
    If the connection fails, returns an empty dict.
    """
    if psycopg2 is None:
        print("psycopg2 is not installed, using an empty schema", file=sys.stderr, flush=True)
        return {}

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Get all tables from the current database
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]

        schema = {}
        for table_name in tables:
            # Get columns and types for each table
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (table_name,))
            columns = {row[0]: row[1].upper() for row in cur.fetchall()}
            schema[table_name] = columns

        cur.close()
        conn.close()

        print(f"Loaded schema from PostgreSQL: {len(schema)} tables", file=sys.stderr, flush=True)
        return schema
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}", file=sys.stderr, flush=True)
        return {}


# Load the schema from PostgreSQL at startup
DB_SCHEMA = fetch_schema_from_postgres()


def _closest(name: str, candidates, cutoff: float = 0.6) -> str | None:
    """Find the closest name among candidates (or None).

    The comparison is case-insensitive. If there is an exact match
    (ignoring case), it is returned immediately without any "fixing".
    Otherwise the closest one by difflib is taken if it passes the cutoff.
    """
    lower_map = {c.lower(): c for c in candidates}
    if name.lower() in lower_map:
        return lower_map[name.lower()]
    matches = difflib.get_close_matches(
        name.lower(), list(lower_map), n=1, cutoff=cutoff
    )
    return lower_map[matches[0]] if matches else None


def fuzzy_correct(ast: exp.Expression, schema: dict) -> list[tuple[str, str, str]]:
    """Fix typos in table and column names against the schema (in-place).

    Returns a list of performed replacements of the form (kind, was, became)
    — convenient for logging. Order matters: tables first (so that the real
    schema in scope is known), then columns within the already fixed tables.
    """
    fixes: list[tuple[str, str, str]] = []

    # 1) Tables. Along the way build a map {alias/name -> real table name}
    #    to later restrict column lookup to the relevant tables.
    scope: dict[str, str] = {}
    for tbl in ast.find_all(exp.Table):
        real = _closest(tbl.name, schema.keys())
        if real and real != tbl.name:
            fixes.append(("table", tbl.name, real))
            tbl.set("this", exp.to_identifier(real, quoted=tbl.this.quoted))
        real = real or tbl.name  # if no match — leave as is
        if real in schema:
            scope[tbl.alias or real] = real
            scope[real] = real

    # 2) Columns. Candidates are the columns of tables in scope; for a
    #    qualified column (t.col) restrict to its own table.
    for col in ast.find_all(exp.Column):
        if col.table:
            tables = [scope[col.table]] if col.table in scope else []
        else:
            tables = list(dict.fromkeys(scope.values()))
        candidates: list[str] = []
        for t in tables:
            candidates.extend(schema.get(t, {}))
        if not candidates:
            continue
        real = _closest(col.name, candidates)
        if real and real != col.name:
            fixes.append(("column", col.name, real))
            col.set("this", exp.to_identifier(real, quoted=col.this.quoted))

    return fixes


def resolve_query(query: str) -> tuple[str, list[tuple[str, str, str]], str]:
    """Resolve (qualify) the query columns against DB_SCHEMA.

    First typos in table/column names are fixed against the metadata, then
    each unqualified column gets its table/alias assigned, tables get
    aliases attached, and `*` is expanded against the schema. The query
    meaning is preserved. On a resolution error the already fixed SQL is
    returned.

    Returns a tuple (final_query, fixes, original_query) — so that the
    calling code can show exactly what was executed.
    """
    original = query
    fixes: list[tuple[str, str, str]] = []
    try:
        ast = sqlglot.parse_one(query)
    except Exception:  # the query does not parse at all
        return query, fixes, original

    try:
        fixes = fuzzy_correct(ast, DB_SCHEMA)
        if fixes:
            print(f"  fuzzy fixes: {fixes}", file=sys.stderr, flush=True)
    except Exception:  # fixing must not take the tool down
        pass

    try:
        resolved = qualify(ast, schema=DB_SCHEMA).sql(pretty=True, normalize=True)
    except Exception:  # ambiguous etc. — return at least the fixed SQL
        resolved = ast.sql(pretty=True, normalize=True)

    return resolved, fixes, original


def format_table(columns, rows):
    """Format results as an ASCII table."""
    if not rows:
        return "Results: 0 rows"

    # Compute column widths
    widths = [len(str(col)) for col in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    # Build the table
    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header = "| " + " | ".join(str(col).ljust(widths[i]) for i, col in enumerate(columns)) + " |"

    lines = [separator, header, separator]
    for row in rows:
        line = "| " + " | ".join(str(val).ljust(widths[i]) for i, val in enumerate(row)) + " |"
        lines.append(line)
    lines.append(separator)

    return "\n".join(lines)


@mcp.tool()
def run_sql(
    query: Annotated[
        str,
        Field(description=(
            "The SQL query to execute. THIS PARAMETER MUST ALREADY BE "
            "SYNTACTICALLY VALID: the model MUST fix all syntax errors in "
            "the original query before calling the tool "
            "(missing VALUES, a missing column list in INSERT, "
            "quotes around reserved words, commas), preserving the meaning. "
            "The server does not fix syntax — it only resolves table/column "
            "names against the schema. Do not pass invalid SQL."
        )),
    ],
) -> str:
    """Execute an SQL query: print it to the screen and write it to the log.

    Typos and syntax errors in the input query should be fixed before
    reaching valid SQL, preserving the original meaning.
    """
    timestamp = datetime.now().isoformat(timespec="seconds")

    # Resolve columns against the schema (assign tables, expand *)
    resolved, fixes, original = resolve_query(query)

    # Show which query was actually executed (after all corrections)
    header_lines = [f"Original query:\n{original}", f"Executed query:\n{resolved}"]
    if fixes:
        parts = []
        for kind, was, became in fixes:
            what = "table" if kind == "table" else "column"
            parts.append(f"{what} '{was}' -> '{became}'")
        header_lines.append("Corrections: " + "; ".join(parts))
    header = "\n".join(header_lines)

    if psycopg2 is None:
        return f"Error: psycopg2 is not installed\n\n{header}"

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Execute the query
        cur.execute(resolved)

        # Get the column description
        if cur.description:
            columns = [desc[0] for desc in cur.description]
            # Get all rows
            rows = cur.fetchall()

            result = format_table(columns, rows)
            cur.close()
            conn.commit()
            conn.close()
            return header + "\n\n" + result
        else:
            # The query returns no results (INSERT, UPDATE, DELETE, etc.)
            affected = cur.rowcount
            cur.close()
            conn.commit()
            conn.close()
            return header + f"\n\nOK: {affected} rows affected"

    except Exception as e:
        return header + f"\n\nDB error: {e}"


if __name__ == "__main__":
    mcp.run()
