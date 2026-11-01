const editor = document.querySelector("#sql-editor");
const tableList = document.querySelector("#table-list");
const resultTitle = document.querySelector("#result-title");
const resultMeta = document.querySelector("#result-meta");
const resultMessage = document.querySelector("#result-message");
const resultsContainer = document.querySelector("#results-container");
const contextMenu = document.querySelector("#context-menu");

let tables = [];
let selectedTable = null;

const examples = {
  "heart-rate": `SELECT event_date, round(avg(bpm), 1) AS average_bpm, count(*) AS observations
FROM heart_rate
WHERE user_id = 'user-0001'
GROUP BY event_date
ORDER BY event_date;`,
};

function showMessage(message, isError = false) {
  resultsContainer.hidden = true;
  resultMessage.hidden = false;
  resultMessage.textContent = message;
  resultMessage.classList.toggle("error", isError);
  resultMeta.textContent = "";
}

function displayTable(columns, rows, title, meta) {
  resultTitle.textContent = title;
  resultMeta.textContent = meta || `${rows.length.toLocaleString()} rows`;
  resultMessage.hidden = true;
  resultsContainer.hidden = false;
  resultsContainer.replaceChildren();

  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const column of columns) {
    const cell = document.createElement("th");
    cell.textContent = column.name || column;
    if (column.type) cell.title = column.type;
    head.appendChild(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      const name = column.name || column;
      const cell = tr.insertCell();
      const value = row[name];
      cell.textContent = value === null || value === undefined
        ? "NULL"
        : typeof value === "object"
          ? JSON.stringify(value)
          : String(value);
    }
  }
  resultsContainer.appendChild(table);
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `Request failed (${response.status})`;
    if (typeof detail === "object" && detail !== null) {
      const error = new Error(detail.message || JSON.stringify(detail));
      Object.assign(error, detail);
      throw error;
    }
    throw new Error(detail);
  }
  return payload;
}

async function loadCatalog() {
  tableList.innerHTML = '<div class="placeholder">Loading tables…</div>';
  try {
    const payload = await requestJson("/api/catalog");
    tables = payload.tables;
    tableList.replaceChildren();
    if (!tables.length) {
      tableList.innerHTML = '<div class="placeholder">No tables found.</div>';
      return;
    }
    for (const table of tables) {
      const button = document.createElement("button");
      button.className = "table-item";
      button.type = "button";
      button.dataset.table = table.name;
      button.innerHTML = '<span class="table-icon">▦</span>';
      const name = document.createElement("span");
      name.className = "table-name";
      name.textContent = table.name;
      const count = document.createElement("span");
      count.className = "table-count";
      count.textContent = `${table.record_count.toLocaleString()} samples`;
      button.append(name, count);
      button.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        selectedTable = table.name;
        document.querySelectorAll(".table-item").forEach((item) => item.classList.toggle("selected", item === button));
        contextMenu.hidden = false;
        contextMenu.style.left = `${Math.min(event.clientX, window.innerWidth - 180)}px`;
        contextMenu.style.top = `${Math.min(event.clientY, window.innerHeight - 130)}px`;
      });
      button.addEventListener("click", () => {
        selectedTable = table.name;
        document.querySelectorAll(".table-item").forEach((item) => item.classList.toggle("selected", item === button));
        editor.value = `SELECT *\nFROM ${table.name}\nLIMIT 100;`;
        editor.focus();
      });
      tableList.appendChild(button);
    }
  } catch (error) {
    tableList.innerHTML = "";
    const message = document.createElement("div");
    message.className = "placeholder";
    message.textContent = error.message;
    tableList.appendChild(message);
  }
}

async function previewTable(tableName) {
  resultTitle.textContent = `${tableName} preview`;
  resultMessage.hidden = false;
  resultMessage.classList.remove("error");
  resultMessage.textContent = "Loading preview…";
  resultsContainer.hidden = true;
  try {
    const payload = await requestJson(`/api/catalog/${encodeURIComponent(tableName)}/preview?limit=20`);
    const table = tables.find((item) => item.name === tableName);
    const columns = table ? table.columns : Object.keys(payload.rows[0] || {});
    displayTable(columns, payload.rows, `${tableName} preview`, `${payload.row_count} rows`);
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function showSchema(tableName) {
  resultTitle.textContent = `${tableName} schema`;
  resultMessage.hidden = false;
  resultMessage.classList.remove("error");
  resultMessage.textContent = "Loading schema…";
  resultsContainer.hidden = true;
  try {
    const payload = await requestJson(`/api/catalog/${encodeURIComponent(tableName)}/schema`);
    displayTable(
      [{ name: "column", type: "COLUMN" }, { name: "type", type: "TYPE" }],
      payload.columns.map((column) => ({ column: column.name, type: column.type })),
      `${tableName} schema`,
      `${payload.columns.length} columns`,
    );
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function runQuery() {
  const button = document.querySelector("#run-query");
  button.disabled = true;
  button.innerHTML = "<span>…</span> Running";
  resultTitle.textContent = "Results";
  resultMessage.hidden = false;
  resultMessage.classList.remove("error");
  resultMessage.textContent = "Running query…";
  resultsContainer.hidden = true;
  const started = performance.now();
  try {
    const payload = await requestJson("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql: editor.value }),
    });
    const elapsed = Math.round(performance.now() - started);
    const suffix = payload.truncated ? " · limited to 1,000 rows" : "";
    displayTable(payload.columns, payload.rows, "Query results", `${payload.row_count} rows · ${elapsed} ms${suffix}`);
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = "<span>▶</span> Run query";
  }
}

async function askQuestion() {
  const input = document.querySelector("#question-input");
  const button = document.querySelector("#ask-query");
  const question = input.value.trim();
  if (!question) {
    showMessage("Enter a question first.", true);
    input.focus();
    return;
  }
  button.disabled = true;
  button.innerHTML = "<span>…</span> Thinking";
  resultTitle.textContent = "Natural-language query";
  resultMessage.hidden = false;
  resultMessage.classList.remove("error");
  resultMessage.textContent = "Asking the local model to generate a read-only query…";
  resultsContainer.hidden = true;
  const started = performance.now();
  try {
    const payload = await requestJson("/api/nl-query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    editor.value = payload.sql;
    const elapsed = Math.round(performance.now() - started);
    const suffix = payload.truncated ? " · limited to 1,000 rows" : "";
    displayTable(
      payload.columns,
      payload.rows,
      "Natural-language results",
      `${payload.row_count} rows · ${elapsed} ms · ${payload.model}${suffix}`,
    );
  } catch (error) {
    if (error.sql) editor.value = error.sql;
    showMessage(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = "<span>✦</span> Ask AI";
  }
}

document.querySelector("#run-query").addEventListener("click", runQuery);
document.querySelector("#ask-query").addEventListener("click", askQuestion);
document.querySelector("#refresh-catalog").addEventListener("click", loadCatalog);
document.querySelector("#example-query").addEventListener("change", (event) => {
  if (examples[event.target.value]) editor.value = examples[event.target.value];
  event.target.value = "";
});
editor.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runQuery();
  }
  if (event.key === "Tab") {
    event.preventDefault();
    const start = editor.selectionStart;
    editor.setRangeText("  ", start, editor.selectionEnd, "end");
  }
});
document.querySelector("#question-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    askQuestion();
  }
});
contextMenu.addEventListener("click", (event) => {
  const action = event.target.dataset.action;
  const tableName = selectedTable;
  contextMenu.hidden = true;
  if (!tableName) return;
  if (action === "preview") previewTable(tableName);
  if (action === "schema") showSchema(tableName);
  if (action === "query") {
    editor.value = `SELECT *\nFROM ${tableName}\nLIMIT 100;`;
    editor.focus();
  }
});
document.addEventListener("click", (event) => {
  if (!contextMenu.contains(event.target)) contextMenu.hidden = true;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") contextMenu.hidden = true;
});

editor.value = examples["heart-rate"];
loadCatalog();
