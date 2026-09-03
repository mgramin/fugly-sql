# fugly-sql

> **Fugly-SQL — because explaining your schema in plain English to an AI is dumber than just writing the damn SQL. Write it wrong, write it fast, we'll correct the rest.**

An MCP server (stdio) with a single tool: `run_sql`. It accepts a SQL query — yes,
even the messy, typo-ridden, "I typed this at 2am" kind — and fixes both the
*syntax* (broken commas, missing `VALUES`, quotes around reserved words) and the
*semantics* (wrong table names, mistyped columns, unresolvable schema) against
your real PostgreSQL schema — including operator names and correctly inferred
joins — then executes the corrected query.

No chatty schema descriptions, no translating your data model into bedtime stories
for an LLM. You already know what you want to query — so just say it, wrong and
fast, and let Fugly-SQL make it right.

## Usage

You don't even need to ask. Just drop a bare SQL query into the chat — no
"run this", no pointing at the tool:

> SELECT * FROM users WHERE id = 1

Fugly-SQL spots that it's a query and figures out that it should be executed,
where, and how — so it runs it. Or ask explicitly, if you prefer:

> Run the SQL query `SELECT * FROM users` via the run_sql tool


### Written fast, written wrong — still works

The whole point: don't bother being precise. Just type the query the way it
spills out of your head — typos, wrong names, missing schema, whatever. Here's
what a *quick* (i.e. sloppy) request looks like:

> run the sql `SELECT nam, emial FROM usres WHERE id = 1` pls

Inside, `run_sql`:

1. Sees you *meant* `name`, `email` and `users` — and fixes all three against
   the real Postgres schema.
2. Qualifies the columns (assigns `users.nam` → `users.name`, resolves `*` if
   you used it) so the query actually runs.
3. Executes it and hands back the result, with the original query, the executed
   query and the fixes spelled out.

Output you'd get back:

```
Original query:
SELECT nam, emial FROM usres WHERE id = 1

Executed query:
SELECT users.name AS name, users.email AS email
FROM users
WHERE users.id = 1

Corrections: table 'usres' -> 'users'; column 'nam' -> 'name'; column 'emial' -> 'email'
```

No schema explaining, no hand-holding. Write it wrong, write it fast — the
server corrects the rest.

### Natural language injections, anywhere in the query

You don't even have to write the query out, let alone produce valid SQL. Drop a
plain-English fragment straight into the query — wherever it reads natural — and
the agent injects the real SQL in its place. That includes the things you *don't*
want: say *what to skip* and the sensitive bits stay out. The schema stays out of
the conversation; the intent stays in it.

A few janky-but-working asks:

```
select everything from users except the password colum
select all columns except password from usres
select * from usres where the boss's id is 1
show me all users and order them by their email alphabetically
```

Each one is translated to proper SQL and then run through the fuzzy fixer. The
`EXCEPT (password)` above turns the `*` into the full column list minus the
sensitive one — so `password` (which you never want dumped into a log or pasted
back into chat) stays out of the result entirely:

```
Original query:
select everything from users except the password colum

Executed query:
SELECT users.id, users.name, users.email
FROM users

Corrections: column 'password' excluded by intent
```

Write it however it comes out of your head — `run_sql` and the agent sort out
the rest.

## Connecting to Claude Code (CLI)

The quickest way is the command:

```powershell
claude mcp add fugly-sql -- python C:\Users\PC\src\fugly-sql\server.py
```

Or manually create a `.mcp.json` file in the project root:

```json
{
  "mcpServers": {
    "fugly-sql": {
      "command": "python",
      "args": ["/path/server.py"]
    }
  }
}
```

Check the connection:

```powershell
claude mcp list
```

Inside a Claude Code session you can view the server status with the `/mcp` command.

## Notes

- Use `python` from the same environment where the `mcp` package is installed.
  If Python is not in `PATH`, specify the full path to `python.exe` in the `command` field.
- In stdio-MCP you cannot write to `stdout` with a regular `print` — this breaks
  the protocol, so the "screen" output goes to `stderr`.

## Installation

```powershell
pip install -r requirements.txt
```

Check that the server starts:

```powershell
python server.py
```

It will silently wait for messages over stdio — that's normal. To stop: `Ctrl+C`.
