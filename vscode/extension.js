// fugly-sql: VS Code client for the fugly_sql.py engine.
//
// Grabs the SQL statement under the cursor (delimited by ';'), POSTs it to a
// running `fugly_sql.py --serve` engine, and inserts the corrected query on the
// line right after the original statement.

const vscode = require("vscode");

// Find the SQL statement under the cursor, delimited by ';'.
// Returns { text, endOffset } where endOffset is the position the statement
// ends at (before the next ';', or end of document).
function currentStatement(fullText, cursorOffset) {
  let start = 0;
  let end = fullText.length;
  let i = fullText.indexOf(";");
  while (i !== -1) {
    if (i <= cursorOffset) {
      start = i + 1; // statement begins after the previous ';'
    } else {
      end = i; // statement ends before the next ';'
      break;
    }
    i = fullText.indexOf(";", i + 1);
  }
  return { text: fullText.slice(start, end).trim(), endOffset: end };
}

async function fix() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return;
  }
  const doc = editor.document;
  const fullText = doc.getText();
  const cursorOffset = doc.offsetAt(editor.selection.active);

  const { text: stmt, endOffset } = currentStatement(fullText, cursorOffset);
  if (!stmt) {
    vscode.window.showWarningMessage("fugly-sql: no SQL statement under the cursor");
    return;
  }

  const url = vscode.workspace.getConfiguration("fugly-sql").get("url");

  let corrected;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "text/plain" },
      body: stmt,
    });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    corrected = (await resp.text()).trim();
  } catch (err) {
    vscode.window.showErrorMessage(`fugly-sql: ${err.message} (is the engine running at ${url}?)`);
    return;
  }
  if (!corrected) {
    vscode.window.showWarningMessage("fugly-sql: empty result");
    return;
  }

  // Terminate with ';' so ';'-based detection stays valid for the next statement.
  if (!/;\s*$/.test(corrected)) {
    corrected += ";";
  }

  // Insert on the line right after the one the statement ends on.
  const endLine = doc.positionAt(endOffset).line;
  const insertPos = doc.lineAt(endLine).range.end;
  await editor.edit((b) => b.insert(insertPos, "\n" + corrected));
}

function activate(context) {
  context.subscriptions.push(vscode.commands.registerCommand("fugly-sql.fix", fix));
}

function deactivate() {}

module.exports = { activate, deactivate };
