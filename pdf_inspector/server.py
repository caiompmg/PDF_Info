"""Inspetor Geométrico de PDF — backend.

Ferramenta isolada de leitura/inspeção: abre um PDF e, ao clicar/selecionar
uma região, monta a ASSINATURA objetiva daquele bloco — o conjunto de
camadas de moldura (imagens de borda/etiqueta reaproveitadas, cada uma por
hash SHA-256, na posição relativa exata entre si) mais a cor de
preenchimento de fundo (amostrada do próprio render, já composta com
qualquer opacidade/gradiente). Imagens minúsculas (linhas/réguas genéricas
reaproveitadas o documento inteiro) são descartadas da assinatura, para não
gerar falso positivo.

Salvar uma assinatura sob uma categoria (H1, H2, STF, STJ, TST) grava UM
único padrão composto em palette.json — não uma entrada solta por camada —
para que "onde no PDF há algo nesses exatos termos" seja uma pergunta que
faz sentido responder depois, por esta ou por outra ferramenta que aplique
a formatação Markdown pré-configurada por categoria (também em
palette.json, como entradas do tipo "formatting").

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
# Imagens com as duas dimensões originais (em pixels) menores ou iguais a
# este valor são descartadas da assinatura: são primitivas genéricas de
# preenchimento (linhas/réguas esticadas), reaproveitadas o documento
# inteiro para fins não relacionados — não distinguem um tipo de bloco.
GENERIC_IMAGE_MAX_DIM = 5
# Tolerância (em pontos PDF) ao comparar a posição relativa de cada camada
# de moldura entre a assinatura atual e um padrão salvo.
REL_BBOX_TOLERANCE = 3.0

# Formatação Markdown padrão sugerida por categoria — semeada em
# palette.json na primeira execução (se não houver nenhuma entrada
# "formatting" ainda) e livre para ser editada depois (na mão ou via
# POST /palette/formatting), tanto por esta ferramenta quanto pela que for
# consumir os padrões salvos para converter PDF em Markdown.
DEFAULT_FORMATTING = {
    "H1": {"markdown_prefix": "# ", "markdown_suffix": ""},
    "H2": {"markdown_prefix": "## ", "markdown_suffix": ""},
    "STF": {"markdown_prefix": "> **STF:** ", "markdown_suffix": ""},
    "STJ": {"markdown_prefix": "> **STJ:** ", "markdown_suffix": ""},
    "TST": {"markdown_prefix": "> **TST:** ", "markdown_suffix": ""},
}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Sessões por upload: {"doc": fitz.Document, "filename": str}. Ferramenta
# local de uso individual: manter em memória é suficiente.
SESSIONS: dict[str, dict] = {}


def load_palette() -> list[dict]:
    """Carrega o palette.json de trabalho (sempre o desta pasta).

    Uma lista plana de entradas tipadas por "type":
    - "block_pattern": um padrão composto salvo via /save — "category",
      "frame_layers" (lista de {"hash", "rel_bbox", "width", "height"},
      posição relativa à origem da seleção) e/ou "fill_rgb".
    - "image" / "color": entradas legadas, curadas manualmente ou salvas
      antes desta versão (compatibilidade; ainda usadas para o
      palette_match individual por camada).
    - "formatting": um template Markdown por categoria.

    Formato inválido é ignorado sem erro (arquivo começa vazio na primeira
    vez). Entradas antigas sem "id" ganham um id na primeira leitura,
    gravado de volta no arquivo para ficar estável dali em diante. Se não
    houver nenhuma entrada "formatting", semeia os padrões default.
    """
    if not PALETTE_PATH.is_file():
        data = []
    else:
        try:
            data = json.loads(PALETTE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        if not isinstance(data, list):
            data = []

    dirty = False
    for entry in data:
        if "id" not in entry:
            entry["id"] = uuid.uuid4().hex[:12]
            dirty = True

    if not any(e.get("type") == "formatting" for e in data):
        for category, fmt in DEFAULT_FORMATTING.items():
            data.append({"id": uuid.uuid4().hex[:12], "type": "formatting", "category": category, **fmt})
        dirty = True

    if dirty:
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
    estável entre páginas/PDFs que reaproveitam a mesma arte.

    Cada resultado traz "generic": True quando a imagem original é
    minúscula dos dois lados (ex.: 2×2px) — sinal de que é uma primitiva
    de preenchimento genérica (linha/régua esticada), não uma moldura
    distintiva, e por isso não entra na assinatura de um padrão salvo.
    """
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
            width, height = extracted["width"], extracted["height"]
            results.append(
                {
                    "hash": sha256,
                    "bbox": [round(v, 2) for v in rect],
                    "width": width,
                    "height": height,
                    "generic": width <= GENERIC_IMAGE_MAX_DIM and height <= GENERIC_IMAGE_MAX_DIM,
                    "palette_match": match_image_hash(sha256),
                }
            )
    return results


def build_signature(target: fitz.Rect, frame: list[dict], fill_rgb) -> dict:
    """Monta a assinatura objetiva de um bloco: as camadas de moldura não
    genéricas, com sua posição relativa à origem da seleção (para o
    padrão valer em qualquer lugar da página em que reaparecer), mais a
    cor de fundo."""
    layers = []
    for image in frame:
        if image["generic"]:
            continue
        bx0, by0, bx1, by1 = image["bbox"]
        layers.append(
            {
                "hash": image["hash"],
                "rel_bbox": [
                    round(bx0 - target.x0, 2),
                    round(by0 - target.y0, 2),
                    round(bx1 - target.x0, 2),
                    round(by1 - target.y0, 2),
                ],
                "width": image["width"],
                "height": image["height"],
            }
        )
    layers.sort(key=lambda layer: (layer["rel_bbox"][0], layer["rel_bbox"][1]))
    return {"frame_layers": layers, "fill_rgb": fill_rgb}


def signature_is_empty(signature: dict) -> bool:
    return not signature["frame_layers"] and is_near_white(signature["fill_rgb"])


def match_pattern(signature: dict):
    """Procura, entre os padrões compostos salvos, um cujos termos batem
    exatamente com a assinatura atual: mesmo conjunto de camadas de
    moldura (por hash), cada uma na mesma posição relativa (com
    tolerância), e/ou a mesma cor de fundo — segundo o que o padrão salvo
    exige. Retorna a entrada do palette, ou None."""
    sig_layers_by_hash = {layer["hash"]: layer for layer in signature["frame_layers"]}
    for entry in PALETTE:
        if entry.get("type") != "block_pattern":
            continue
        pattern_layers = entry.get("frame_layers") or []
        if pattern_layers:
            if set(sig_layers_by_hash) != {layer["hash"] for layer in pattern_layers}:
                continue
            if any(
                any(
                    abs(a - b) > REL_BBOX_TOLERANCE
                    for a, b in zip(sig_layers_by_hash[layer["hash"]]["rel_bbox"], layer["rel_bbox"])
                )
                for layer in pattern_layers
            ):
                continue
        pattern_rgb = entry.get("fill_rgb")
        if pattern_rgb is not None:
            if signature["fill_rgb"] is None or any(
                abs(a - b) > COLOR_TOLERANCE for a, b in zip(signature["fill_rgb"], pattern_rgb)
            ):
                continue
        elif not pattern_layers:
            continue  # padrão sem moldura e sem cor não é um termo válido para bater
        return entry
    return None


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
    (imagens), a cor de fundo de uma região, monta a assinatura (moldura
    não genérica + posição relativa + cor) e busca um padrão salvo que
    bata exatamente com ela."""
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

    signature = build_signature(target, frame, fill_rgb)
    pattern = match_pattern(signature)

    return {
        "frame": frame,
        "fill_color": fill_color,
        "expanded_by_margin": expanded,
        "signature": signature,
        "pattern_match": (
            {"id": pattern["id"], "name": pattern["name"], "category": pattern["category"]}
            if pattern
            else None
        ),
    }


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
        "pattern_match": result["pattern_match"],
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
    signature = result["signature"]
    if signature_is_empty(signature):
        abort(400, description="Nada de distintivo nessa região (sem moldura reconhecível nem cor de fundo) para salvar.")

    already = any(
        e.get("type") == "block_pattern"
        and e.get("category") == category
        and e.get("frame_layers") == signature["frame_layers"]
        and e.get("fill_rgb") == signature["fill_rgb"]
        for e in PALETTE
    )
    if already:
        return jsonify({"added": [], "already_existed": True})

    count = sum(1 for e in PALETTE if e.get("type") == "block_pattern" and e.get("category") == category)
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": f"{category}_pattern_{count + 1}",
        "category": category,
        "type": "block_pattern",
        "frame_layers": signature["frame_layers"],
        "fill_rgb": signature["fill_rgb"],
        "source_pdf": filename,
        "page": page_number,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    PALETTE.append(entry)
    save_palette(PALETTE)
    return jsonify({"added": [entry], "already_existed": False})


@app.post("/palette/formatting")
def set_formatting():
    """Cria ou atualiza o template Markdown de uma categoria."""
    body = request.get_json(silent=True) or {}
    category = body.get("category")
    if category not in CATEGORIES:
        abort(400, description=f"category deve ser um de {', '.join(CATEGORIES)}.")
    prefix = body.get("markdown_prefix", "")
    suffix = body.get("markdown_suffix", "")
    entry = next((e for e in PALETTE if e.get("type") == "formatting" and e.get("category") == category), None)
    if entry is None:
        entry = {"id": uuid.uuid4().hex[:12], "type": "formatting", "category": category}
        PALETTE.append(entry)
    entry["markdown_prefix"] = prefix
    entry["markdown_suffix"] = suffix
    save_palette(PALETTE)
    return jsonify(entry)


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
