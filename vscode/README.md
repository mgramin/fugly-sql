# fugly-sql (VS Code)

VS Code client for the [`fugly_sql.py`](../fugly_sql.py) engine. Takes the SQL
statement under the cursor (delimited by `;`), sends it to a running engine, and
inserts the corrected query on the line right after the original.

## Prerequisites

Run the engine's HTTP server (keeps the model warm, schema cached):

```
python C:/Users/PC/src/fugly-sql/fugly_sql.py --serve 127.0.0.1:8000
```

## Run it (development)

1. Open the `vscode/` folder in VS Code.
2. Press `F5` — this launches an Extension Development Host with the extension
   loaded (no build step; it's plain JS).
3. In the new window open a `.sql` file, put the cursor inside a statement, and
   run **Fugly: Fix SQL under cursor** (Command Palette) or press `Ctrl+Alt+F`.

## Install permanently

Package to a `.vsix` and install:

```
npm install -g @vscode/vsce
cd vscode && vsce package
code --install-extension fugly-sql-0.1.0.vsix
```

## Settings

- `fugly-sql.url` — engine endpoint (default `http://127.0.0.1:8000/fix`).

## Notes

- Statements are split on `;`; terminate them with `;`. The inserted result is
  terminated automatically so the next statement is still detected correctly.
- Same engine as the Neovim client — one backend, thin clients.
