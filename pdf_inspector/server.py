"""Inspetor Geométrico de PDF — backend.

Ferramenta isolada de leitura/inspeção: abre um PDF, renderiza páginas e
retorna dados estruturais objetivos (cor de preenchimento, cor/espessura de
borda, bounding box, hash SHA-256 de imagens) da região clicada/selecionada.

Fora de escopo: OCR, formatação, edição do documento, qualquer chamada a IA.

Uso:
    python server.py [--port 5000]
    # instala Flask e PyMuPDF sozinho na primeira vez, se faltarem
    # abrir http://localhost:5000 no navegador
"""

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path


def _ensure_dependencies() -> None:
    """Instala Flask/PyMuPDF via pip se não estiverem disponíveis.

    Evita obrigar quem só quer usar a ferramenta a rodar
    `pip install -r requirements.txt` manualmente antes.
    """
    required = {"fitz": "PyMuPDF>=1.24,<2.0", "flask": "Flask>=3.0,<4.0"}
    missing = [spec for module, spec in required.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print(f"Instalando dependências ausentes: {', '.join(missing)}...", file=sys.stderr)
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_dependencies()

import fitz  # noqa: E402  (PyMuPDF; import após garantir a instalação)
from flask import Flask, abort, jsonify, request, send_file, send_from_directory  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Tolerância por canal (0-1) ao comparar cores com o palette.json.
COLOR_TOLERANCE = 0.02
# Margem (em pontos PDF) usada para expandir a busca quando nada é
# encontrado na região exata.
SEARCH_MARGIN_PT = 2.0

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Documentos abertos, por sessão. Ferramenta local de uso individual:
# manter em memória é suficiente e evita escrever o PDF em disco.
SESSIONS: dict[str, fitz.Document] = {}


def load_palette() -> list[dict]:
    """Carrega o palette.json conhecido, se existir (raiz do repo ou aqui).

    Cada entrada esperada: {"name": ..., "rgb": [r,g,b], ...} para vetores
    ou {"name": ..., "hash": "sha256...", ...} para imagens. Entradas em
    outros formatos são ignoradas sem erro.
    """
    for candidate in (BASE_DIR / "palette.json", BASE_DIR.parent / "palette.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # aceita também {"nome": {...}, ...}
                return [{"name": k, **v} for k, v in data.items() if isinstance(v, dict)]
    return []


PALETTE = load_palette()


def match_color(rgb, palette=None, tolerance=COLOR_TOLERANCE):
    """Retorna o nome da entrada do palette cuja cor bate com `rgb`.

    Comparação por canal com tolerância fixa; None se não houver match.
    """
    if rgb is None:
        return None
    entries = PALETTE if palette is None else palette
    for entry in entries:
        ref = entry.get("rgb")
        if not ref or len(ref) != len(rgb):
            continue
        if all(abs(a - b) <= tolerance for a, b in zip(rgb, ref)):
            return entry.get("name") or entry.get("type")
    return None


def match_image_hash(sha256_hex):
    """Retorna o nome da entrada do palette com o mesmo hash de imagem."""
    for entry in PALETTE:
        if entry.get("hash") == sha256_hex:
            return entry.get("name") or entry.get("type")
    return None


def normalize_color(color):
    """Converte a cor do get_drawings() para lista RGB [0-1] arredondada."""
    if color is None:
        return None
    values = list(color)
    if len(values) == 1:  # tom de cinza
        values = values * 3
    return [round(v, 4) for v in values[:3]]


def get_document(session_id: str) -> fitz.Document:
    doc = SESSIONS.get(session_id or "")
    if doc is None:
        abort(404, description="Sessão não encontrada. Envie o PDF novamente.")
    return doc


def get_page(doc: fitz.Document, page_number: int) -> fitz.Page:
    """Páginas são 1-based na API (como exibido ao usuário)."""
    if not 1 <= page_number <= doc.page_count:
        abort(404, description=f"Página {page_number} fora do intervalo (1-{doc.page_count}).")
    return doc[page_number - 1]


def region_hits(rect: fitz.Rect, target: fitz.Rect) -> bool:
    """True se `rect` cobre a região alvo.

    Um clique vira um rect degenerado (área zero), e `Rect.intersects()`
    é sempre falso para rects vazios — nesse caso testa se o ponto está
    contido em `rect`.
    """
    if target.is_empty:
        return target.top_left in rect
    return rect.intersects(target)


def collect_vectors(page: fitz.Page, target: fitz.Rect) -> list[dict]:
    """Desenhos vetoriais cujo rect intersecta a região alvo.

    Resposta já no formato que o palette.json espera para vetores
    ({"rgb": [...]}) — ver Fase 2 da spec.
    """
    results = []
    for drawing in page.get_drawings():
        if not region_hits(drawing["rect"], target):
            continue
        rect = drawing["rect"]
        fill = normalize_color(drawing.get("fill"))
        stroke = normalize_color(drawing.get("color"))
        width = drawing.get("width")
        results.append(
            {
                "fill_rgb": fill,
                "stroke_rgb": stroke,
                "stroke_width": round(width, 4) if width is not None else None,
                "bbox": [round(v, 2) for v in rect],
                "palette_match": match_color(fill),
            }
        )
    return results


def collect_images(doc: fitz.Document, page: fitz.Page, target: fitz.Rect) -> list[dict]:
    """Imagens cujo rect intersecta a região alvo, com hash SHA-256 dos bytes
    originais (estável entre páginas/PDFs que embutem a mesma imagem)."""
    results = []
    seen = set()
    for image_info in page.get_images(full=True):
        xref = image_info[0]
        for rect in page.get_image_rects(xref):
            if not region_hits(rect, target):
                continue
            key = (xref, tuple(round(v, 2) for v in rect))
            if key in seen:
                continue
            seen.add(key)
            extracted = doc.extract_image(xref)
            sha256 = hashlib.sha256(extracted["image"]).hexdigest()
            results.append(
                {
                    "hash": sha256,
                    "bbox": [round(v, 2) for v in rect],
                    "width": extracted["width"],
                    "height": extracted["height"],
                    "palette_match": match_image_hash(sha256),
                }
            )
    return results


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.post("/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        abort(400, description="Nenhum arquivo enviado (campo 'file').")
    data = file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        abort(400, description="Arquivo não pôde ser aberto como PDF.")
    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = doc
    return jsonify({"session_id": session_id, "num_pages": doc.page_count})


@app.get("/page/<int:page_number>/render")
def render_page(page_number):
    doc = get_document(request.args.get("session_id"))
    page = get_page(doc, page_number)
    try:
        dpi = int(request.args.get("dpi", 150))
    except ValueError:
        abort(400, description="dpi inválido.")
    if not 36 <= dpi <= 600:
        abort(400, description="dpi fora do intervalo permitido (36-600).")
    pixmap = page.get_pixmap(dpi=dpi)
    from io import BytesIO

    return send_file(BytesIO(pixmap.tobytes("png")), mimetype="image/png")


@app.post("/page/<int:page_number>/inspect")
def inspect(page_number):
    body = request.get_json(silent=True) or {}
    doc = get_document(body.get("session_id") or request.args.get("session_id"))
    page = get_page(doc, page_number)

    # Aceita {x, y} (clique) ou {x0, y0, x1, y1} (seleção), em pontos PDF.
    if all(k in body for k in ("x0", "y0", "x1", "y1")):
        try:
            target = fitz.Rect(body["x0"], body["y0"], body["x1"], body["y1"])
        except (TypeError, ValueError):
            abort(400, description="Coordenadas de seleção inválidas.")
        target.normalize()
    elif "x" in body and "y" in body:
        try:
            x, y = float(body["x"]), float(body["y"])
        except (TypeError, ValueError):
            abort(400, description="Coordenadas de clique inválidas.")
        target = fitz.Rect(x, y, x, y)
    else:
        abort(400, description="Informe {x, y} ou {x0, y0, x1, y1} em pontos PDF.")

    vectors = collect_vectors(page, target)
    images = collect_images(doc, page, target)
    expanded = False
    if not vectors and not images:
        # Nada na região exata: expande a busca com uma margem pequena
        # antes de responder "nada encontrado".
        target = fitz.Rect(target) + (
            -SEARCH_MARGIN_PT,
            -SEARCH_MARGIN_PT,
            SEARCH_MARGIN_PT,
            SEARCH_MARGIN_PT,
        )
        vectors = collect_vectors(page, target)
        images = collect_images(doc, page, target)
        expanded = True

    # Amostra de texto sempre com uma margem mínima, senão um clique
    # pontual (rect degenerado) não recorta nada.
    text_clip = fitz.Rect(target)
    if text_clip.width < SEARCH_MARGIN_PT or text_clip.height < SEARCH_MARGIN_PT:
        text_clip += (-SEARCH_MARGIN_PT, -SEARCH_MARGIN_PT, SEARCH_MARGIN_PT, SEARCH_MARGIN_PT)
    text_sample = page.get_text(clip=text_clip).strip()

    response = {
        "query": {
            "page": page_number,
            "rect": [round(v, 2) for v in target],
            "expanded_by_margin": expanded,
        },
        "vectors": vectors,
        "images": images,
        "text_sample": text_sample,
    }
    if not vectors and not images and not text_sample:
        response["message"] = "Nada encontrado nesta região (mesmo com margem de busca)."
    return jsonify(response)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
def api_error(error):
    return jsonify({"error": error.description}), error.code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspetor Geométrico de PDF")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
