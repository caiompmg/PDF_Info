# Inspetor Geométrico de PDF

Ferramenta isolada de leitura/inspeção de PDF. Abre um PDF e mostra todas as
páginas empilhadas para leitura por rolagem. Ao clicar (ou arrastar uma
seleção) sobre um bloco, identifica dois padrões objetivos daquele ponto:

- **Moldura estilizada**: imagens de borda/etiqueta (cabeçalho, etiqueta de
  jurisprudência etc.) que cobrem a região, cada uma com hash SHA-256 dos
  bytes originais — estável entre páginas/PDFs que reaproveitam a mesma
  arte.
- **Cor de preenchimento de fundo do bloco**: amostrada diretamente do
  render (já composta com opacidade, gradiente etc.), usando várias
  amostras ao redor do ponto para não cair em cima de texto.

Escolhida uma categoria (**H1, H2, STF, STJ, TST**) no painel lateral, o
botão **Salvar** grava esse padrão em `palette.json`, consolidando uma base
de dados para outra ferramenta usar na formatação automática.

Objetivo: subsidiar, sem IA em nenhuma etapa, a criação de um `palette.json`
com os padrões visuais recorrentes de headers e blocos de jurisprudência.
Fora de escopo: OCR, formatação, edição do documento.

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

1. Envie o PDF pelo botão de upload — as páginas carregam empilhadas, role
   para navegar entre elas.
2. Clique num ponto (ou arraste um retângulo) sobre um bloco.
3. O painel lateral mostra a moldura e a cor de fundo encontradas.
4. Escolha a categoria (H1/H2/STF/STJ/TST) e clique em **Salvar** para
   gravar esse padrão em `palette.json`. Salvar o mesmo padrão de novo não
   duplica a entrada.

Se nada for encontrado na região exata, o backend expande a busca de
moldura com uma margem de 2 pt antes de responder "nada encontrado" (o
campo `query.expanded_by_margin` indica quando isso aconteceu).

## Endpoints

```
POST /upload                          multipart/form-data (file=<pdf>)
                                      -> { "session_id": ..., "num_pages": ... }

GET  /page/<n>/render?dpi=150&session_id=...   -> image/png

POST /page/<n>/inspect                body: { "session_id": ..., "dpi": 150,
                                              "x": ..., "y": ... }        (clique)
                                        ou  { "session_id": ..., "dpi": 150,
                                              "x0": ..., "y0": ...,
                                              "x1": ..., "y1": ... }      (seleção)
                                      coordenadas em pontos PDF (px * 72 / dpi)

POST /page/<n>/save                   mesmo corpo do /inspect, mais
                                      { "category": "H1"|"H2"|"STF"|"STJ"|"TST" }
                                      -> { "added": [...], "already_existed": bool }
```

Resposta do `/inspect`:

```json
{
  "query": { "page": 1, "rect": [x0, y0, x1, y1], "expanded_by_margin": false },
  "frame": [
    { "hash": "sha256...", "bbox": [x0, y0, x1, y1],
      "width": 84, "height": 35, "palette_match": "jurisprudencia_stj_etiqueta_base" }
  ],
  "fill_color": { "rgb": [0.94, 0.85, 0.69], "hex": "#f0d9b0", "palette_match": null }
}
```

## palette.json

Vive em `pdf_inspector/palette.json`, sempre nesta pasta (é o arquivo que o
próprio `/save` lê e grava). Cada entrada tem `name`, `category` (uma de
H1/H2/STF/STJ/TST — as entradas curadas iniciais não têm essa chave) e
`type`:

```json
[
  { "name": "header_band", "type": "image", "hash": "sha256..." },
  { "name": "STJ_fill_11", "category": "STJ", "type": "color", "rgb": [0.94, 0.85, 0.69] }
]
```

Ao inspecionar, a moldura é comparada por hash exato e a cor de fundo por
RGB com tolerância de 0.02 por canal; o nome da entrada correspondente
aparece em `palette_match`.
