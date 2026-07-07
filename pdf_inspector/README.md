# Inspetor Geométrico de PDF

Ferramenta isolada de leitura/inspeção de PDF. Abre um PDF e mostra todas as
páginas empilhadas para leitura por rolagem. Ao clicar (ou arrastar uma
seleção) sobre um bloco, monta a **assinatura** objetiva daquele bloco —
"onde no PDF há algo nesses exatos termos":

- **Moldura estilizada**: as imagens de borda/etiqueta (cabeçalho, etiqueta
  de jurisprudência etc.) que cobrem a região, cada uma por hash SHA-256 dos
  bytes originais, na **posição relativa exata** entre si. Imagens
  minúsculas (≤5×5px, como as linhas/réguas genéricas que o exportador
  reaproveita o documento inteiro pra desenhar qualquer traço) são
  descartadas — elas não distinguem um tipo de bloco do outro, e entrariam
  como falso positivo.
- **Cor de preenchimento de fundo do bloco**: amostrada diretamente do
  render (já composta com opacidade, gradiente etc.), usando várias
  amostras ao redor do ponto para não cair em cima de texto.

Escolhida uma categoria (**H1, H2, STF, STJ, TST**) no painel lateral, o
botão **Salvar** grava essa assinatura como **um único padrão composto**
em `palette.json` — não uma entrada solta por camada de imagem, para não
perder a relação "esse conjunto de elementos, nessa posição relativa entre
si, é que forma esse tipo de bloco". Da próxima vez que a mesma combinação
aparecer em qualquer lugar do documento (ou de outro PDF do mesmo modelo),
o `/inspect` reconhece e devolve `pattern_match`.

Cada categoria também tem um template Markdown associado (`palette.json`,
entradas do tipo `formatting`) — para que outra ferramenta, ao converter o
PDF pra Markdown, saiba que formatação aplicar quando reconhecer aquele
padrão.

Objetivo: subsidiar, sem IA em nenhuma etapa, uma base de dados precisa o
bastante para automatizar a formatação de PDF → Markdown. Fora de escopo:
OCR, a própria conversão/edição do documento, qualquer chamada a IA.

## Ferramenta subsidiária: onde ela entra no pipeline

Este inspetor não decide sozinho a formatação de nada — ele é consultado
pela ferramenta de conversão principal (que já usa regras de regex e
tipografia sobre o texto real extraído), e só tem autoridade numa fatia
específica do problema:

| Situação | Quem decide |
|---|---|
| Header 3 ou inferior | A ferramenta principal — são texto real, a identificação tipográfica já é confiável. O inspetor **nem é consultado**. |
| Callout de tribunal **já identificado** pelas regras de texto | A ferramenta principal — o inspetor não tem autoridade para mudar o que já foi identificado. |
| Callout de tribunal **não identificado** pelas regras de texto | O inspetor — verifica se a moldura/cor da região bate com um padrão STF/STJ/TST salvo e preenche a lacuna (ou confirma que não é nada). |
| Header 1 ou 2 | O inspetor tem autoridade plena para **homologar ou corrigir** o palpite da ferramenta principal. H1 e H2, neste modelo de documento, são sempre imagem — não têm texto selecionável nem metadado tipográfico, exatamente o ponto cego que a assinatura geométrica cobre. |

Na prática, isso significa que `pattern_match` (ver `/inspect` abaixo) é o
único sinal que o inspetor produz — bateu com um padrão salvo de categoria
X, ou não bateu com nada (`null`). A ferramenta principal decide o que
fazer com o `null` (deixar como estava, rebaixar, marcar para revisão);
o inspetor não tenta adivinhar isso, porque só enxerga geometria e cor,
nunca o texto em si.

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
3. O painel lateral mostra a moldura encontrada (camadas genéricas aparecem
   esmaecidas, como "linha genérica") e a cor de fundo. Se a assinatura
   já bater com um padrão salvo, aparece um selo **padrão salvo: categoria**.
4. Escolha a categoria (H1/H2/STF/STJ/TST) e clique em **Salvar** para
   gravar essa assinatura em `palette.json`. Salvar a mesma assinatura de
   novo não duplica a entrada; salvar uma região sem moldura reconhecível
   nem cor de fundo distinta é rejeitado (nada para individualizar ali).
5. A seção **Padrões salvos**, embaixo, lista tudo que já está em
   `palette.json` — agrupado por categoria e, dentro de cada grupo,
   ordenado por data de criação (as entradas curadas manualmente, sem
   data, aparecem primeiro). Ela carrega assim que a página abre, mesmo
   sem PDF enviado. Marque uma ou mais entradas e clique em **Excluir
   selecionados** para removê-las de `palette.json`.

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
                                      -> { "added": [entry] | [], "already_existed": bool }
                                      (400 se a região não tiver nada distintivo)

GET  /palette                         -> { "entries": [...] }  (todo o palette.json)

POST /palette/delete                  body: { "ids": ["id1", "id2", ...] }
                                      -> { "removed": int, "remaining": int }

POST /palette/formatting              body: { "category": "H1", "markdown_prefix": "# ",
                                              "markdown_suffix": "" }
                                      -> a entrada "formatting" criada/atualizada
```

Resposta do `/inspect`:

```json
{
  "query": { "page": 1, "rect": [x0, y0, x1, y1], "expanded_by_margin": false },
  "frame": [
    { "hash": "sha256...", "bbox": [x0, y0, x1, y1], "width": 84, "height": 35,
      "generic": false, "palette_match": "jurisprudencia_stj_etiqueta_base" }
  ],
  "fill_color": { "rgb": [0.94, 0.85, 0.69], "hex": "#f0d9b0", "palette_match": null },
  "pattern_match": { "id": "...", "name": "H1_pattern_1", "category": "H1" } | null
}
```

`pattern_match` só vem preenchido quando **todos** os termos do padrão
salvo batem: mesmo conjunto de camadas de moldura (por hash, nenhuma a
mais nem a menos), cada uma na mesma posição relativa (tolerância de 3pt),
e a mesma cor de fundo (tolerância de 0.02 por canal) quando o padrão
salvo também exige cor.

## palette.json

Vive em `pdf_inspector/palette.json`, sempre nesta pasta (é o arquivo que
`/save`, `/palette/delete` e `/palette/formatting` leem e gravam). Lista
plana de entradas tipadas por `type`:

- **`block_pattern`** — um padrão composto salvo via `/save`: `category`,
  `frame_layers` (lista de `{"hash", "rel_bbox", "width", "height"}`, a
  posição relativa à origem da seleção quando foi salvo) e/ou `fill_rgb`.
- **`formatting`** — um template Markdown por categoria (`markdown_prefix`
  / `markdown_suffix`), semeado com um padrão razoável na primeira execução
  se ainda não existir nenhum, e editável via `/palette/formatting`.
- **`image`** / **`color`** — entradas legadas (curadas manualmente antes
  desta versão, ou salvas por uma versão anterior da ferramenta). Continuam
  funcionando para o `palette_match` individual por camada de imagem/cor,
  mas não participam do `pattern_match` composto — que exige `block_pattern`.

```json
[
  { "id": "2bbca2266aff", "name": "header_band", "type": "image", "hash": "sha256..." },
  { "id": "9deaef9636ce", "type": "formatting", "category": "H1",
    "markdown_prefix": "# ", "markdown_suffix": "" },
  { "id": "3e5a4b60", "name": "H1_pattern_1", "category": "H1", "type": "block_pattern",
    "frame_layers": [
      { "hash": "sha256...", "rel_bbox": [0, 0, 594.5, 49.3], "width": 1651, "height": 137 },
      { "hash": "sha256...", "rel_bbox": [-0.4, 9.2, 462.4, 41.0], "width": 1285, "height": 88 }
    ],
    "fill_rgb": [0.94, 0.85, 0.69], "created_at": "2026-07-07T12:12:53+00:00" }
]
```

Todas as entradas ganham um `id` estável (retroativo, na primeira leitura,
para as que não tinham) — é por ele que `/palette/delete` remove.
