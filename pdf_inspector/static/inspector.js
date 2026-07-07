// Inspetor Geométrico de PDF — frontend.
// Upload do PDF, render da página num canvas 1:1 com o pixmap do backend,
// captura de clique/seleção e exibição do JSON de inspeção.

"use strict";

const fileInput = document.getElementById("file-input");
const prevBtn = document.getElementById("prev-page");
const nextBtn = document.getElementById("next-page");
const pageLabel = document.getElementById("page-label");
const dpiSelect = document.getElementById("dpi-select");
const statusEl = document.getElementById("status");
const canvas = document.getElementById("page-canvas");
const ctx = canvas.getContext("2d");
const resultEl = document.getElementById("result");
const swatchesEl = document.getElementById("swatches");
const copyBtn = document.getElementById("copy-btn");

const state = {
  sessionId: null,
  numPages: 0,
  page: 1,          // 1-based, igual à API
  dpi: 150,
  pageImage: null,  // ImageBitmap da página renderizada
  lastJson: null,
  drag: null,       // {x0, y0, x1, y1} em pixels do canvas, durante o arrasto
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
    state.page = 1;
    setStatus(`"${file.name}" — ${data.num_pages} página(s).`);
    await renderPage();
  } catch (err) {
    setStatus(`Falha no upload: ${err.message}`);
  }
}

async function renderPage() {
  if (!state.sessionId) return;
  state.dpi = parseInt(dpiSelect.value, 10);
  setStatus(`Renderizando página ${state.page}…`);
  const url = `/page/${state.page}/render?dpi=${state.dpi}&session_id=${state.sessionId}`;
  const response = await fetch(url);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    setStatus(`Falha ao renderizar: ${data.error || response.status}`);
    return;
  }
  const bitmap = await createImageBitmap(await response.blob());
  state.pageImage = bitmap;
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  drawPage();
  updatePager();
  setStatus(`Página ${state.page}/${state.numPages} @ ${state.dpi} dpi ` +
            `(${bitmap.width}×${bitmap.height}px). Clique ou arraste para inspecionar.`);
}

function drawPage(selection) {
  if (!state.pageImage) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(state.pageImage, 0, 0);
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

function updatePager() {
  pageLabel.textContent = `${state.page} / ${state.numPages}`;
  prevBtn.disabled = state.page <= 1;
  nextBtn.disabled = state.page >= state.numPages;
}

function canvasPos(event) {
  const rect = canvas.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

async function inspect(body) {
  body.session_id = state.sessionId;
  setStatus("Inspecionando…");
  try {
    const data = await fetchJson(`/page/${state.page}/inspect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showResult(data);
    setStatus(`Página ${state.page}/${state.numPages} @ ${state.dpi} dpi.`);
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
  for (const vec of data.vectors || []) {
    for (const [label, rgb] of [["fill", vec.fill_rgb], ["stroke", vec.stroke_rgb]]) {
      if (!rgb) continue;
      const css = `rgb(${rgb.map((v) => Math.round(v * 255)).join(",")})`;
      const el = document.createElement("span");
      el.className = "swatch";
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.background = css;
      el.appendChild(chip);
      el.appendChild(document.createTextNode(
        `${label} (${rgb.join(", ")})${vec.palette_match ? " = " + vec.palette_match : ""}`
      ));
      swatchesEl.appendChild(el);
    }
  }
  for (const img of data.images || []) {
    const el = document.createElement("span");
    el.className = "swatch";
    el.textContent = `img ${img.width}×${img.height} ${img.hash.slice(0, 12)}…` +
      (img.palette_match ? ` = ${img.palette_match}` : "");
    swatchesEl.appendChild(el);
  }
}

// --- Eventos ---

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) uploadPdf(file);
});

prevBtn.addEventListener("click", () => {
  if (state.page > 1) { state.page -= 1; renderPage(); }
});

nextBtn.addEventListener("click", () => {
  if (state.page < state.numPages) { state.page += 1; renderPage(); }
});

dpiSelect.addEventListener("change", () => renderPage());

canvas.addEventListener("mousedown", (event) => {
  if (!state.sessionId || !state.pageImage) return;
  const { x, y } = canvasPos(event);
  state.drag = { x0: x, y0: y, x1: x, y1: y };
});

canvas.addEventListener("mousemove", (event) => {
  if (!state.drag) return;
  const { x, y } = canvasPos(event);
  state.drag.x1 = x;
  state.drag.y1 = y;
  drawPage(state.drag);
});

window.addEventListener("mouseup", (event) => {
  if (!state.drag) return;
  const drag = state.drag;
  state.drag = null;
  drawPage();
  const dx = Math.abs(drag.x1 - drag.x0);
  const dy = Math.abs(drag.y1 - drag.y0);
  if (dx < CLICK_THRESHOLD_PX && dy < CLICK_THRESHOLD_PX) {
    inspect({ x: pxToPt(drag.x0), y: pxToPt(drag.y0) });
  } else {
    inspect({
      x0: pxToPt(Math.min(drag.x0, drag.x1)),
      y0: pxToPt(Math.min(drag.y0, drag.y1)),
      x1: pxToPt(Math.max(drag.x0, drag.x1)),
      y1: pxToPt(Math.max(drag.y0, drag.y1)),
    });
  }
});

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
