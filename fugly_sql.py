"""A simple SQL fixer engine.

Fixes an SQL query's syntax with a local LLM (qwen2.5-coder:1.5b via Ollama),
then resolves table/column names against the PostgreSQL schema.

Two client-facing modes:

  * stdin -> stdout (default): read a query from stdin, print the corrected
    query to stdout. Diagnostics go to stderr so stdout stays clean.
  * --serve: run a persistent HTTP server that keeps the model warm and the
    schema cached, so each request pays only the LLM cost (no process spawn).
    POST the raw SQL body to '/fix'; the corrected query comes back as text.
"""

import difflib
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.qualify import qualify

try:
    import psycopg2
except ImportError:
    psycopg2 = None


# LLM configuration — any OpenAI-compatible chat-completions endpoint
# (Ollama by default; also OpenAI, vLLM, LM Studio, ...). Override via env.
LLM_URL = os.environ.get("FUGLY_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
LLM_MODEL = os.environ.get("FUGLY_LLM_MODEL", "qwen2.5-coder:1.5b")
LLM_API_KEY = os.environ.get("FUGLY_LLM_API_KEY", "")  # optional; Ollama ignores it

FIX_SYSTEM_PROMPT = (
    "You are an SQL syntax fixer. You are given a single SQL query that may "
    "contain syntax errors. Turn it into a syntactically valid form, "
    "preserving the original meaning. Typical errors to fix: missing keywords "
    "(VALUES), a missing column list in INSERT, unescaped reserved words such "
    "as table, extra/missing commas. "
    "When an INSERT is missing its column list, take the REAL column names in "
    "order from the schema below for that table (skip an auto-generated serial "
    "'id' unless a value is clearly provided for it); never invent placeholder "
    "names like column1, column2. Do NOT fix typos in table/column names — "
    "leave them as is. "
    "Return ONLY the corrected SQL, with no explanation, comments, or code "
    "fences."
)


def schema_prompt(schema: dict) -> str:
    """Render the live DB schema as compact lines for the LLM prompt."""
    if not schema:
        return "(schema unavailable)"
    lines = []
    for table, cols in schema.items():
        col_list = ", ".join(f"{c} {t}" for c, t in cols.items())
        lines.append(f"{table}({col_list})")
    return "\n".join(lines)


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

        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]

        schema = {}
        for table_name in tables:
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


def fix_syntax_with_llm(query: str, schema: dict) -> str:
    """Fix the query syntax using the local model (Ollama).

    The live DB schema is injected into the prompt so the model uses real
    column names (e.g. when adding a missing INSERT column list) instead of
    inventing placeholders.

    On any error (model unavailable, bad response) the original query is
    returned unchanged so the schema-resolution step can still run.
    """
    user_prompt = (
        f"Schema:\n{schema_prompt(schema)}\n\n"
        f"Query:\n{query}\n\nCorrected SQL:"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": FIX_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 256,     # SQL is short — cap generation
        # Ollama extensions (ignored by strict OpenAI servers):
        "keep_alive": -1,      # keep the model resident — no reload per call
        "options": {"num_ctx": 1024},  # small context = faster prefill
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    try:
        req = urllib.request.Request(
            LLM_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        fixed = (data["choices"][0]["message"]["content"] or "").strip()
        # Strip accidental code fences the model may add.
        if fixed.startswith("```"):
            fixed = fixed.strip("`")
            if fixed.lower().startswith("sql"):
                fixed = fixed[3:]
            fixed = fixed.strip()
        return fixed or query
    except Exception as e:
        print(f"LLM fix failed ({e}), using the original query", file=sys.stderr, flush=True)
        return query


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


def resolve_query(query: str, schema: dict) -> tuple[str, list[tuple[str, str, str]]]:
    """Resolve (qualify) the query columns against the schema.

    First typos in table/column names are fixed against the metadata, then
    each unqualified column gets its table/alias assigned, tables get
    aliases attached, and `*` is expanded against the schema. The query
    meaning is preserved. On a resolution error the already fixed SQL is
    returned.

    Returns a tuple (final_query, fixes).
    """
    fixes: list[tuple[str, str, str]] = []
    try:
        ast = sqlglot.parse_one(query)
    except Exception:  # the query does not parse at all
        return query, fixes

    try:
        fixes = fuzzy_correct(ast, schema)
        if fixes:
            print(f"  fuzzy fixes: {fixes}", file=sys.stderr, flush=True)
    except Exception:  # fixing must not take the tool down
        pass

    try:
        resolved = qualify(ast, schema=schema).sql(pretty=True, normalize=True)
    except Exception:  # ambiguous etc. — return at least the fixed SQL
        resolved = ast.sql(pretty=True, normalize=True)

    return resolved, fixes


def fix_query(query: str, schema: dict) -> tuple[str, list[tuple[str, str, str]]]:
    """Core engine step: LLM syntax fix + schema resolution.

    Returns (resolved_query, fixes). Shared by every client (stdin, HTTP, ...).
    """
    # 1) Fix syntax with the local model (the job Claude used to do).
    fixed = fix_syntax_with_llm(query, schema)
    if fixed != query:
        print(f"LLM syntax fix:\n{fixed}", file=sys.stderr, flush=True)

    # 2) Resolve table/column names against the schema.
    resolved, fixes = resolve_query(fixed, schema)
    if fixes:
        parts = [f"{kind} '{was}' -> '{became}'" for kind, was, became in fixes]
        print("Schema corrections: " + "; ".join(parts), file=sys.stderr, flush=True)

    return resolved, fixes


def run_stdin() -> None:
    """CLI mode: one query from stdin, corrected query to stdout."""
    query = sys.stdin.read().strip()
    if not query:
        print("Empty input", file=sys.stderr, flush=True)
        return

    schema = fetch_schema_from_postgres()
    resolved, _ = fix_query(query, schema)
    print(resolved)  # the only thing on stdout


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Persistent HTTP mode: fetch the schema once, keep the model warm.

    POST the raw SQL body to any path (e.g. '/fix'); the response body is the
    corrected query as plain text. The schema is loaded at startup and reused.
    """
    schema = fetch_schema_from_postgres()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            query = self.rfile.read(length).decode("utf-8").strip()
            if not query:
                self.send_error(400, "Empty query")
                return
            try:
                resolved, _ = fix_query(query, schema)
            except Exception as e:  # never take the server down on one bad query
                self.send_error(500, str(e))
                return
            body = resolved.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep stderr quiet
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"fugly-sql serving on http://{host}:{port} ({len(schema)} tables cached)",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def main() -> None:
    if "--serve" in sys.argv:
        idx = sys.argv.index("--serve")
        # optional "host:port" after --serve
        host, port = "127.0.0.1", 8000
        if idx + 1 < len(sys.argv):
            addr = sys.argv[idx + 1]
            if ":" in addr:
                host, port_str = addr.rsplit(":", 1)
                port = int(port_str)
        serve(host, port)
    else:
        run_stdin()


if __name__ == "__main__":
    main()
