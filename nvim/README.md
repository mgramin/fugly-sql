# fugly-sql.nvim

Neovim client for the [`fugly_sql.py`](../fugly_sql.py) engine. It takes the SQL
statement under the cursor (delimited by `;`), pipes it through the engine, and
inserts the corrected query on the line(s) right after the original.

Requires Neovim ≥ 0.10 (uses `vim.system`).

## Backends

Two ways to reach the engine (`config.url` wins if set):

- **HTTP (recommended)** — run a persistent server once; each fix is just an
  HTTP POST (~0.3s, no process spawn, model kept warm, schema cached):

  ```
  python C:/Users/PC/src/fugly-sql/fugly_sql.py --serve 127.0.0.1:8000
  ```

- **Spawn** — no server; each fix spawns `python fugly_sql.py` (~0.45s). Set
  `python` + `script` instead of `url`.

## Install

Point your plugin manager at this `nvim/` directory and call `setup()`.

**lazy.nvim (HTTP backend)**

```lua
{
  dir = "C:/Users/PC/src/fugly-sql/nvim", -- or a git repo/subpath
  config = function()
    require("fugly-sql").setup({
      url = "http://127.0.0.1:8000/fix",       -- a running `--serve` engine
      command = "FuglyFix",                    -- optional, this is the default
      keymap = "<leader>ff",                   -- optional normal-mode mapping
    })
  end,
}
```

For the spawn backend instead, drop `url` and set
`python = "python"`, `script = "C:/Users/PC/src/fugly-sql/fugly_sql.py"`.

## Usage

Put the cursor anywhere inside a SQL statement and run:

```
:FuglyFix
```

The corrected query is inserted on the next line. Set `keymap` in `setup()` to
bind it (e.g. `keymap = "<leader>ff"`), or map it yourself:

```lua
vim.keymap.set("n", "<leader>ff", "<cmd>FuglyFix<cr>", { desc = "Fix SQL under cursor" })
```

## Notes

- Statements are split on `;`. If the statement under the cursor has no trailing
  `;`, it merges with the following one — terminate statements with `;`.
- The engine needs its runtime running (Ollama with the model, PostgreSQL for the
  schema). The call is async, so Neovim never blocks while it runs.
- HTTP backend uses `curl` (ships with Windows 10+ and most Unixes).
