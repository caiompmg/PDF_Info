"""Inspetor Geométrico de PDF — backend.

Ferramenta isolada de leitura/inspeção: abre um PDF e, ao clicar/selecionar
uma região, identifica dois padrões objetivos daquele ponto — a moldura
estilizada (imagens de borda/etiqueta reaproveitadas, por hash SHA-256) e a
cor de preenchimento de fundo do bloco (amostrada do próprio render, já
composta com qualquer opacidade/gradiente). Serve para marcar blocos como
H1, H2, STF, STJ ou TST e consolidar esses padrões em palette.json, para uso
por outra ferramenta de formatação.

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
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
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
PALETTE_PATH = BASE_DIR / "palette.json"

# Categorias que o painel de inspeção permite atribuir a um bloco.
CATEGORIES = ("H1", "H2", "STF", "STJ", "TST")

# Tolerância por canal (0-1) ao comparar cores com o palette.json.
COLOR_TOLERANCE = 0.02
# Margem (em pontos PDF) usada para expandir a busca de moldura quando nada
# é encontrado na região exata.
SEARCH_MARGIN_PT = 2.0
# Recuo (em pontos PDF) a partir das bordas de uma seleção ao amostrar a cor
# de fundo, para não pegar a própria moldura/borda decorativa.
FILL_SAMPLE_INSET_PT = 3.0

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Sessões por upload: {"doc": fitz.Document, "filename": str}. Ferramenta
# local de uso individual: manter em memória é suficiente.
SESSIONS: dict[str, dict] = {}


def load_palette() -> list[dict]:
    """Carrega o palette.json de trabalho (sempre o desta pasta).

    Cada entrada tem "id" (identificador estável, para selecionar/excluir),
    "name", "category" (uma de CATEGORIES) e "type" ("image", com "hash",
    ou "color", com "rgb"). Formato inválido é ignorado sem erro (arquivo
    começa vazio na primeira vez). Entradas antigas sem "id" (ex.: as
    curadas manualmente) ganham um id na primeira leitura, gravado de volta
    no arquivo para ficar estável dali em diante.
    """
    if not PALETTE_PATH.is_file():
        return []
    try:
        data = json.loads(PALETTE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    missing_id = False
    for entry in data:
        if "id" not in entry:
            entry["id"] = uuid.uuid4().hex[:12]
            missing_id = True
    if missing_id:
        save_palette(data)
    return data


def save_palette(entries: list[dict]) -> None:
    PALETTE_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


PALETTE = load_palette()


def match_color(rgb, tolerance=COLOR_TOLERANCE):
    """Retorna o nome da entrada do palette cuja cor bate com `rgb`."""
    if rgb is None:
        return None
    for entry in PALETTE:
        ref = entry.get("rgb")
        if not ref or len(ref) != len(rgb):
            continue
        if all(abs(a - b) <= tolerance for a, b in zip(rgb, ref)):
            return entry.get("name")
    return None


def match_image_hash(sha256_hex):
    """Retorna o nome da entrada do palette com o mesmo hash de imagem."""
    for entry in PALETTE:
        if entry.get("hash") == sha256_hex:
            return entry.get("name")
    return None


def get_session(session_id: str) -> dict:
    session = SESSIONS.get(session_id or "")
    if session is None:
        abort(404, description="Sessão não encontrada. Envie o PDF novamente.")
    return session


def get_document(session_id: str) -> fitz.Document:
    return get_session(session_id)["doc"]


def get_page(doc: fitz.Document, page_number: int) -> fitz.Page:
    """Páginas são 1-based na API (como exibido ao usuário)."""
    if not 1 <= page_number <= doc.page_count:
        abort(404, description=f"Página {page_number} fora do intervalo (1-{doc.page_count}).")
    return doc[page_number - 1]


def parse_target(body: dict) -> fitz.Rect:
    """Lê {x, y} (clique) ou {x0, y0, x1, y1} (seleção) do corpo, em pontos PDF."""
    if all(k in body for k in ("x0", "y0", "x1", "y1")):
        try:
            target = fitz.Rect(body["x0"], body["y0"], body["x1"], body["y1"])
        except (TypeError, ValueError):
            abort(400, description="Coordenadas de seleção inválidas.")
        target.normalize()
        return target
    if "x" in body and "y" in body:
        try:
            x, y = float(body["x"]), float(body["y"])
        except (TypeError, ValueError):
            abort(400, description="Coordenadas de clique inválidas.")
        return fitz.Rect(x, y, x, y)
    abort(400, description="Informe {x, y} ou {x0, y0, x1, y1} em pontos PDF.")


def parse_dpi(body: dict) -> int:
    try:
        dpi = int(body.get("dpi", 150))
    except (TypeError, ValueError):
        abort(400, description="dpi inválido.")
    if not 36 <= dpi <= 600:
        abort(400, description="dpi fora do intervalo permitido (36-600).")
    return dpi


def region_hits(rect: fitz.Rect, target: fitz.Rect) -> bool:
    """True se `rect` cobre a região alvo.

    Um clique vira um rect degenerado (área zero), e `Rect.intersects()`
    é sempre falso para rects vazios — nesse caso testa se o ponto está
    contido em `rect`.
    """
    if target.is_empty:
        return target.top_left in rect
    return rect.intersects(target)


def collect_frame(doc: fitz.Document, page: fitz.Page, target: fitz.Rect) -> list[dict]:
    """Imagens (molduras/etiquetas estilizadas) cujo rect intersecta a
    região alvo, identificadas por hash SHA-256 dos bytes originais —
    estável entre páginas/PDFs que reaproveitam a mesma arte."""
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


def sample_pixel(page: fitz.Page, x: float, y: float, dpi: int):
    """Lê a cor renderizada (já composta: opacidade, gradiente etc.) num
    único ponto do PDF, sem redesenhar a página inteira."""
    if not page.rect.contains(fitz.Point(x, y)):
        return None
    clip = fitz.Rect(x - 0.5, y - 0.5, x + 0.5, y + 0.5)
    pixmap = page.get_pixmap(dpi=dpi, clip=clip)
    if pixmap.width == 0 or pixmap.height == 0:
        return None
    color = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)
    return tuple(round(c / 255, 3) for c in color[:3])


def sample_fill_color(page: fitz.Page, target: fitz.Rect, dpi: int) -> list[float] | None:
    """Amostra a cor de fundo do bloco: centro + um anel de 8 pontos ao
    redor, usando a cor mais frequente entre as amostras como defesa
    contra cair em cima de texto — vale tanto para um clique pontual
    (raio fixo pequeno) quanto para uma seleção arrastada (raio baseado
    no tamanho do retângulo, recuado para não pegar a moldura)."""
    cx = (target.x0 + target.x1) / 2
    cy = (target.y0 + target.y1) / 2
    if target.width > 2 * FILL_SAMPLE_INSET_PT and target.height > 2 * FILL_SAMPLE_INSET_PT:
        radius = min(target.width, target.height) / 2 - FILL_SAMPLE_INSET_PT
    else:
        radius = FILL_SAMPLE_INSET_PT
    ring_offsets = [(0, 0), (-radius, 0), (radius, 0), (0, -radius), (0, radius),
                    (-radius, -radius), (radius, -radius), (-radius, radius), (radius, radius)]
    points = [(cx + dx, cy + dy) for dx, dy in ring_offsets]
    samples = [c for c in (sample_pixel(page, px, py, dpi) for px, py in points) if c is not None]
    if not samples:
        return None
    most_common = Counter(samples).most_common(1)[0][0]
    return list(most_common)


def is_near_white(rgb, tolerance=0.03) -> bool:
    return rgb is not None and all(v >= 1 - tolerance for v in rgb)


def inspect_region(doc: fitz.Document, page: fitz.Page, target: fitz.Rect, dpi: int) -> dict:
    """Núcleo compartilhado por /inspect e /save: encontra a moldura
    (imagens) e a cor de fundo de uma região."""
    frame = collect_frame(doc, page, target)
    expanded = False
    if not frame:
        expanded_target = fitz.Rect(target) + (
            -SEARCH_MARGIN_PT,
            -SEARCH_MARGIN_PT,
            SEARCH_MARGIN_PT,
            SEARCH_MARGIN_PT,
        )
        frame = collect_frame(doc, page, expanded_target)
        expanded = bool(frame)

    fill_rgb = sample_fill_color(page, target, dpi)
    fill_color = None
    if fill_rgb is not None:
        fill_color = {
            "rgb": fill_rgb,
            "hex": "#{:02x}{:02x}{:02x}".format(*(round(v * 255) for v in fill_rgb)),
            "palette_match": match_color(fill_rgb),
        }

    return {"frame": frame, "fill_color": fill_color, "expanded_by_margin": expanded}


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
    SESSIONS[session_id] = {"doc": doc, "filename": file.filename}
    return jsonify({"session_id": session_id, "num_pages": doc.page_count})


@app.get("/page/<int:page_number>/render")
def render_page(page_number):
    doc = get_document(request.args.get("session_id"))
    page = get_page(doc, page_number)
    dpi = parse_dpi(request.args)
    pixmap = page.get_pixmap(dpi=dpi)
    return send_file(BytesIO(pixmap.tobytes("png")), mimetype="image/png")


@app.post("/page/<int:page_number>/inspect")
def inspect(page_number):
    body = request.get_json(silent=True) or {}
    doc = get_document(body.get("session_id"))
    page = get_page(doc, page_number)
    target = parse_target(body)
    dpi = parse_dpi(body)

    result = inspect_region(doc, page, target, dpi)
    response = {
        "query": {
            "page": page_number,
            "rect": [round(v, 2) for v in target],
            "expanded_by_margin": result["expanded_by_margin"],
        },
        "frame": result["frame"],
        "fill_color": result["fill_color"],
    }
    if not result["frame"] and is_near_white(result["fill_color"]["rgb"] if result["fill_color"] else None):
        response["message"] = "Nada encontrado nesta região (mesmo com margem de busca)."
    return jsonify(response)


@app.post("/page/<int:page_number>/save")
def save_entry(page_number):
    body = request.get_json(silent=True) or {}
    session = get_session(body.get("session_id"))
    doc, filename = session["doc"], session["filename"]
    page = get_page(doc, page_number)
    target = parse_target(body)
    dpi = parse_dpi(body)

    category = body.get("category")
    if category not in CATEGORIES:
        abort(400, description=f"category deve ser um de {', '.join(CATEGORIES)}.")

    result = inspect_region(doc, page, target, dpi)
    added = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for image in result["frame"]:
        already = any(e.get("category") == category and e.get("hash") == image["hash"] for e in PALETTE)
        if already:
            continue
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": f"{category}_frame_{len(PALETTE) + len(added) + 1}",
            "category": category,
            "type": "image",
            "hash": image["hash"],
            "size_ref": [image["width"], image["height"]],
            "source_pdf": filename,
            "page": page_number,
            "created_at": now,
        }
        PALETTE.append(entry)
        added.append(entry)

    fill_color = result["fill_color"]
    if fill_color is not None and not is_near_white(fill_color["rgb"]):
        already = any(
            e.get("category") == category
            and e.get("type") == "color"
            and e.get("rgb")
            and all(abs(a - b) <= COLOR_TOLERANCE for a, b in zip(e["rgb"], fill_color["rgb"]))
            for e in PALETTE
        )
        if not already:
            entry = {
                "id": uuid.uuid4().hex[:12],
                "name": f"{category}_fill_{len(PALETTE) + len(added) + 1}",
                "category": category,
                "type": "color",
                "rgb": fill_color["rgb"],
                "source_pdf": filename,
                "page": page_number,
                "created_at": now,
            }
            PALETTE.append(entry)
            added.append(entry)

    if added:
        save_palette(PALETTE)

    return jsonify({"added": added, "already_existed": not added})


@app.get("/palette")
def get_palette():
    """Lista todas as entradas salvas, para o navegador de padrões do
    painel lateral (agrupar por categoria, ordenar por created_at)."""
    return jsonify({"entries": PALETTE})


@app.post("/palette/delete")
def delete_palette_entries():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        abort(400, description="Informe 'ids' (lista de identificadores a excluir).")
    ids = set(ids)
    before = len(PALETTE)
    PALETTE[:] = [entry for entry in PALETTE if entry.get("id") not in ids]
    removed = before - len(PALETTE)
    if removed:
        save_palette(PALETTE)
    return jsonify({"removed": removed, "remaining": len(PALETTE)})


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
