# fugly-sql

A very simple MCP server (stdio) with a single tool `run_sql`, which
accepts a SQL query, prints it to the screen (to `stderr`), writes it to the
`queries.log` log file, and returns it back to the client.

## Installation

```powershell
pip install -r requirements.txt
```

Check that the server starts:

```powershell
python server.py
```

It will silently wait for messages over stdio — that's normal. To stop: `Ctrl+C`.

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
      "args": ["C:\\Users\\PC\\src\\fugly-sql\\server.py"]
    }
  }
}
```

Check the connection:

```powershell
claude mcp list
```

Inside a Claude Code session you can view the server status with the `/mcp` command.

## Connecting to Claude Desktop

Open the config (Windows):

```
%APPDATA%\Claude\claude_desktop_config.json
```

Add the server:

```json
{
  "mcpServers": {
    "fugly-sql": {
      "command": "python",
      "args": ["C:\\Users\\PC\\src\\fugly-sql\\server.py"]
    }
  }
}
```

Save the file and **restart Claude Desktop**. After the restart, the `run_sql`
tool will appear in the list of available tools (the hammer icon).

## Usage

Ask Claude something like:

> Run the SQL query `SELECT * FROM users` via the run_sql tool

Claude will call `run_sql`, the query will be shown in the server's `stderr`
and written to the `queries.log` file.

## Notes

- Use `python` from the same environment where the `mcp` package is installed.
  If Python is not in `PATH`, specify the full path to `python.exe` in the `command` field.
- In stdio-MCP you cannot write to `stdout` with a regular `print` — this breaks
  the protocol, so the "screen" output goes to `stderr`.
