// Inspetor Geométrico de PDF — frontend.
// Upload do PDF, leitura por rolagem (todas as páginas empilhadas, cada
// canvas 1:1 com o pixmap do backend), captura de clique/seleção por
// página, e painel lateral com o resultado (moldura + cor de fundo) e o
// botão de salvar a categoria escolhida em palette.json.

"use strict";

const fileInput = document.getElementById("file-input");
const dpiSelect = document.getElementById("dpi-select");
const statusEl = document.getElementById("status");
const canvasWrap = document.getElementById("canvas-wrap");
const emptyHint = document.getElementById("empty-hint");
const resultEl = document.getElementById("result");
const swatchesEl = document.getElementById("swatches");
const copyBtn = document.getElementById("copy-btn");
const categorySelect = document.getElementById("category-select");
const saveBtn = document.getElementById("save-btn");
const saveStatusEl = document.getElementById("save-status");

const state = {
  sessionId: null,
  numPages: 0,
  dpi: 150,
  lastJson: null,
  lastQuery: null,   // { page, body } usado para reenviar ao /save
  drag: null,        // { pageBlock, x0, y0, x1, y1 } em pixels do canvas, durante o arrasto
};

// Distância mínima (px) para um arrasto contar como seleção e não clique.
const CLICK_THRESHOLD_PX = 4;

function setStatus(text) {
  statusEl.textContent = text;
}

function pxToPt(px) {
  // Canvas no tamanho exato do pixmap: escala fixa e conhecida 72/dpi.
  return (px * 72) / state.dpi;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Erro HTTP ${response.status}`);
  }
  return data;
}

async function uploadPdf(file) {
  setStatus("Enviando PDF…");
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await fetchJson("/upload", { method: "POST", body: form });
    state.sessionId = data.session_id;
    state.numPages = data.num_pages;
    setStatus(`"${file.name}" — ${data.num_pages} página(s). Carregando…`);
    await renderAllPages();
  } catch (err) {
    setStatus(`Falha no upload: ${err.message}`);
  }
}

async function renderAllPages() {
  state.dpi = parseInt(dpiSelect.value, 10);
  canvasWrap.innerHTML = "";
  clearActivePage();
  for (let pageNumber = 1; pageNumber <= state.numPages; pageNumber++) {
    setStatus(`Carregando página ${pageNumber}/${state.numPages}…`);
    try {
      await renderOnePage(pageNumber);
    } catch (err) {
      setStatus(`Falha ao renderizar página ${pageNumber}: ${err.message}`);
      return;
    }
  }
  setStatus(`${state.numPages} página(s) @ ${state.dpi} dpi. Clique ou arraste sobre um bloco para inspecionar.`);
}

async function renderOnePage(pageNumber) {
  const url = `/page/${pageNumber}/render?dpi=${state.dpi}&session_id=${state.sessionId}`;
  const response = await fetch(url);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || String(response.status));
  }
  const bitmap = await createImageBitmap(await response.blob());

  const block = document.createElement("div");
  block.className = "page-block";
  block.dataset.page = String(pageNumber);

  const label = document.createElement("div");
  label.className = "page-label";
  label.textContent = `Página ${pageNumber}`;

  const canvas = document.createElement("canvas");
  canvas.className = "page-canvas";
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0);
  canvas._bitmap = bitmap;

  attachCanvasEvents(canvas, block, pageNumber);

  block.appendChild(label);
  block.appendChild(canvas);
  canvasWrap.appendChild(block);
}

function clearActivePage() {
  canvasWrap.querySelectorAll(".page-block.active").forEach((el) => el.classList.remove("active"));
}

function redrawPage(canvas, selection) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(canvas._bitmap, 0, 0);
  if (selection) {
    const x = Math.min(selection.x0, selection.x1);
    const y = Math.min(selection.y0, selection.y1);
    const w = Math.abs(selection.x1 - selection.x0);
    const h = Math.abs(selection.y1 - selection.y0);
    ctx.save();
    ctx.strokeStyle = "#3b5bdb";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 3]);
    ctx.fillStyle = "rgba(59, 91, 219, 0.12)";
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
  }
}

function canvasPos(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function attachCanvasEvents(canvas, block, pageNumber) {
  canvas.addEventListener("mousedown", (event) => {
    const { x, y } = canvasPos(canvas, event);
    state.drag = { canvas, block, pageNumber, x0: x, y0: y, x1: x, y1: y };
  });

  canvas.addEventListener("mousemove", (event) => {
    if (!state.drag || state.drag.canvas !== canvas) return;
    const { x, y } = canvasPos(canvas, event);
    state.drag.x1 = x;
    state.drag.y1 = y;
    redrawPage(canvas, state.drag);
  });
}

window.addEventListener("mouseup", () => {
  const drag = state.drag;
  if (!drag) return;
  state.drag = null;
  redrawPage(drag.canvas, null);

  clearActivePage();
  drag.block.classList.add("active");

  const dx = Math.abs(drag.x1 - drag.x0);
  const dy = Math.abs(drag.y1 - drag.y0);
  const body = dx < CLICK_THRESHOLD_PX && dy < CLICK_THRESHOLD_PX
    ? { x: pxToPt(drag.x0), y: pxToPt(drag.y0) }
    : {
        x0: pxToPt(Math.min(drag.x0, drag.x1)),
        y0: pxToPt(Math.min(drag.y0, drag.y1)),
        x1: pxToPt(Math.max(drag.x0, drag.x1)),
        y1: pxToPt(Math.max(drag.y0, drag.y1)),
      };
  inspect(drag.pageNumber, body);
});

async function inspect(pageNumber, body) {
  body.session_id = state.sessionId;
  body.dpi = state.dpi;
  saveBtn.disabled = true;
  saveStatusEl.textContent = "";
  setStatus("Inspecionando…");
  try {
    const data = await fetchJson(`/page/${pageNumber}/inspect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.lastQuery = { page: pageNumber, body };
    showResult(data);
    saveBtn.disabled = false;
    setStatus(`${state.numPages} página(s) @ ${state.dpi} dpi.`);
  } catch (err) {
    setStatus(`Falha na inspeção: ${err.message}`);
  }
}

function showResult(data) {
  state.lastJson = JSON.stringify(data, null, 2);
  resultEl.classList.remove("hint");
  resultEl.textContent = state.lastJson;
  copyBtn.disabled = false;
  renderSwatches(data);
}

function renderSwatches(data) {
  swatchesEl.innerHTML = "";
  const fc = data.fill_color;
  if (fc) {
    const css = `rgb(${fc.rgb.map((v) => Math.round(v * 255)).join(",")})`;
    const el = document.createElement("span");
    el.className = "swatch";
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.background = css;
    el.appendChild(chip);
    el.appendChild(document.createTextNode(
      `fundo ${fc.hex}${fc.palette_match ? " = " + fc.palette_match : ""}`
    ));
    swatchesEl.appendChild(el);
  }
  for (const img of data.frame || []) {
    const el = document.createElement("span");
    el.className = "swatch";
    el.textContent = `moldura ${img.width}×${img.height} ${img.hash.slice(0, 12)}…` +
      (img.palette_match ? ` = ${img.palette_match}` : "");
    swatchesEl.appendChild(el);
  }
}

async function saveEntry() {
  if (!state.lastQuery) return;
  const { page, body } = state.lastQuery;
  const category = categorySelect.value;
  saveBtn.disabled = true;
  saveStatusEl.textContent = "Salvando…";
  try {
    const data = await fetchJson(`/page/${page}/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, category }),
    });
    saveStatusEl.textContent = data.already_existed
      ? `Esse padrão já estava salvo em ${category}.`
      : `Salvo em ${category}: ${data.added.length} entrada(s) nova(s) em palette.json.`;
    saveStatusEl.style.color = "var(--ok)";
  } catch (err) {
    saveStatusEl.textContent = `Falha ao salvar: ${err.message}`;
    saveStatusEl.style.color = "#c92a2a";
  } finally {
    saveBtn.disabled = false;
  }
}

// --- Eventos ---

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) {
    emptyHint.remove();
    uploadPdf(file);
  }
});

dpiSelect.addEventListener("change", () => {
  if (state.sessionId) renderAllPages();
});

saveBtn.addEventListener("click", saveEntry);

copyBtn.addEventListener("click", async () => {
  if (!state.lastJson) return;
  try {
    await navigator.clipboard.writeText(state.lastJson);
    copyBtn.textContent = "Copiado!";
  } catch {
    // Fallback para contextos sem clipboard API (http simples).
    const textarea = document.createElement("textarea");
    textarea.value = state.lastJson;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
    copyBtn.textContent = "Copiado!";
  }
  setTimeout(() => { copyBtn.textContent = "Copiar JSON"; }, 1500);
});
