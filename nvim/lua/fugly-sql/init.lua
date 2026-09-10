-- fugly-sql: Neovim client for the fugly_sql.py engine.
--
-- Grabs the SQL statement under the cursor (delimited by ';'), sends it to the
-- engine, and inserts the corrected query on the line(s) right after the
-- original statement. The call is async, so the editor never blocks.
--
-- Two backends (config.url wins if set):
--   * url:            POST to a running `fugly_sql.py --serve` (fast, no spawn)
--   * python+script:  spawn `python fugly_sql.py` per call (no server needed)

local M = {}

M.config = {
  url = nil, -- e.g. "http://127.0.0.1:8000/fix"; if set, used instead of spawning
  python = "python", -- interpreter used to run the engine (spawn fallback)
  script = nil, -- absolute path to fugly_sql.py (spawn fallback)
  command = "FuglyFix", -- name of the user command to create
  keymap = nil, -- optional normal-mode mapping, e.g. "<leader>ff"
}

-- Count newlines in a string (how many line breaks precede an offset).
local function count_nl(s)
  local n = 0
  for _ in s:gmatch("\n") do
    n = n + 1
  end
  return n
end

-- Return the whole buffer as text plus the cursor's absolute byte offset.
local function buf_text_and_offset()
  local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
  local row, col = unpack(vim.api.nvim_win_get_cursor(0)) -- row 1-indexed, col 0-indexed
  local offset = 0
  for i = 1, row - 1 do
    offset = offset + #lines[i] + 1 -- +1 for the newline
  end
  offset = offset + col
  return table.concat(lines, "\n"), offset
end

-- Find the SQL statement under the cursor, delimited by ';'.
-- Returns (statement_text, insert_row) where insert_row is the 0-indexed
-- position at which the corrected query should be inserted (i.e. right after
-- the line the statement ends on).
local function current_statement()
  local text, offset = buf_text_and_offset()

  local stmt_start = 1
  local stmt_end = #text
  local i = text:find(";", 1, true)
  while i do
    if i <= offset then
      stmt_start = i + 1 -- statement begins after the previous ';'
    else
      stmt_end = i - 1 -- statement ends before the next ';'
      break
    end
    i = text:find(";", i + 1, true)
  end

  local stmt = text:sub(stmt_start, stmt_end)
  -- Insert after the line the statement ends on.
  local insert_row = 1 + count_nl(text:sub(1, stmt_end))
  return stmt, insert_row
end

-- Insert the engine's output after the statement.
local function insert_result(out, insert_row)
  out = vim.trim(out or "")
  if out == "" then
    vim.notify("fugly-sql: empty result", vim.log.levels.WARN)
    return
  end
  local result_lines = vim.split(out, "\n", { plain = true })
  -- Terminate with ';' so ';'-based statement detection stays valid for the
  -- next statement (otherwise the inserted query merges with the following one).
  local last = #result_lines
  if not result_lines[last]:match(";%s*$") then
    result_lines[last] = result_lines[last] .. ";"
  end
  vim.api.nvim_buf_set_lines(0, insert_row, insert_row, false, result_lines)
end

-- The command that sends the statement to the engine, per backend.
local function engine_cmd(stmt)
  if M.config.url then
    -- POST the raw SQL body via curl (fast, no python spawn).
    return { "curl", "-s", "-X", "POST", "--data-binary", stmt, M.config.url }, nil
  end
  -- Fallback: spawn the engine and feed the query on stdin.
  return { M.config.python, M.config.script }, stmt
end

-- Run the engine and insert its output after the statement.
local function run(stmt, insert_row)
  local cmd, stdin = engine_cmd(stmt)
  vim.system(
    cmd,
    { stdin = stdin, text = true },
    vim.schedule_wrap(function(obj)
      if obj.code ~= 0 then
        local msg = (obj.stderr ~= "" and obj.stderr) or ("exit code " .. obj.code)
        vim.notify("fugly-sql: " .. msg, vim.log.levels.ERROR)
        return
      end
      insert_result(obj.stdout, insert_row)
    end)
  )
end

-- Public entry point: fix the statement under the cursor.
function M.fix()
  if not M.config.url and not M.config.script then
    vim.notify("fugly-sql: set config.url or config.script", vim.log.levels.ERROR)
    return
  end
  local stmt, insert_row = current_statement()
  stmt = vim.trim(stmt)
  if stmt == "" then
    vim.notify("fugly-sql: no SQL statement under the cursor", vim.log.levels.WARN)
    return
  end
  run(stmt, insert_row)
end

function M.setup(opts)
  M.config = vim.tbl_extend("force", M.config, opts or {})
  vim.api.nvim_create_user_command(M.config.command, function()
    M.fix()
  end, { desc = "Fix the SQL statement under the cursor with fugly_sql" })

  if M.config.keymap then
    vim.keymap.set("n", M.config.keymap, M.fix, { desc = "Fugly: fix SQL under cursor" })
  end
end

return M
