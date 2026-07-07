# Inspetor Geométrico de PDF

Ferramenta isolada de leitura/inspeção de PDF. Abre um PDF, renderiza as
páginas e, ao clicar (ou arrastar uma seleção) sobre a página, mostra os
dados estruturais objetivos daquela região:

- **Desenhos vetoriais**: cor de preenchimento (RGB 0–1), cor e espessura de
  borda, bounding box em pontos PDF.
- **Imagens**: hash SHA-256 dos bytes originais embutidos (estável entre
  páginas/PDFs que usam a mesma imagem), bounding box e dimensões.
- **Amostra de texto** extraída da região (`page.get_text(clip=...)`).

Objetivo: subsidiar a criação manual e informada de entradas em
`palette.json`, sem IA em nenhuma etapa. Fora de escopo: OCR, formatação,
edição do documento.

## Uso

Requer Python 3.9 ou superior. Nenhuma dependência de sistema é necessária
além de Flask e PyMuPDF — o PyMuPDF já embute o motor de renderização (não
precisa de `poppler`, `ghostscript` etc.).

```bash
python server.py            # http://localhost:5000
```

Não precisa instalar nada antes: o próprio `server.py` verifica se Flask e
PyMuPDF estão presentes e, se não estiverem, instala sozinho (via
`pip install`) antes de iniciar. Se preferir instalar manualmente ou usar
um ambiente virtual, `pip install -r requirements.txt` continua funcionando
normalmente.

1. Envie o PDF pelo botão de upload.
2. Navegue entre as páginas; ajuste o DPI de render se quiser mais zoom.
3. Clique num ponto (ou arraste um retângulo) sobre a página.
4. O painel lateral mostra o JSON retornado, com botão **Copiar JSON**.

Se nada for encontrado na região exata, o backend expande a busca com uma
margem de 2 pt antes de responder "nada encontrado" (o campo
`query.expanded_by_margin` indica quando isso aconteceu).

## Endpoints

```
POST /upload                          multipart/form-data (file=<pdf>)
                                      -> { "session_id": ..., "num_pages": ... }

GET  /page/<n>/render?dpi=150&session_id=...   -> image/png

POST /page/<n>/inspect                body: { "session_id": ...,
                                              "x": ..., "y": ... }        (clique)
                                        ou  { "session_id": ...,
                                              "x0": ..., "y0": ...,
                                              "x1": ..., "y1": ... }      (seleção)
                                      coordenadas em pontos PDF (px * 72 / dpi)
```

Resposta do `/inspect`:

```json
{
  "query": { "page": 1, "rect": [x0, y0, x1, y1], "expanded_by_margin": false },
  "vectors": [
    { "fill_rgb": [r, g, b], "stroke_rgb": [r, g, b],
      "stroke_width": 1.0, "bbox": [x0, y0, x1, y1],
      "palette_match": "questao" }
  ],
  "images": [
    { "hash": "sha256...", "bbox": [x0, y0, x1, y1],
      "width": 800, "height": 120, "palette_match": null }
  ],
  "text_sample": "texto extraído da região"
}
```

## palette.json (opcional)

Se existir um `palette.json` ao lado do `server.py` (ou na raiz do repo),
as cores e hashes encontrados são comparados com ele e o nome da entrada
correspondente aparece em `palette_match`. Formatos aceitos:

```json
[
  { "name": "questao", "rgb": [0.84, 0.82, 0.92], "type": "vector", "scope": "..." },
  { "name": "header_decor", "hash": "sha256...", "type": "image" }
]
```

ou um objeto `{ "nome": { ... } }`. A comparação de cor usa tolerância de
0.02 por canal.

## Fase 2 (planejada, não implementada)

Botão "adicionar ao palette.json" no painel lateral. O JSON do `/inspect`
já sai no formato compatível (`rgb`/`hash` prontos para virar entrada),
para facilitar esse passo depois.
