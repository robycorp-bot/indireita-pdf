# Endireita PDF

API em **Python + FastAPI + OpenCV + PyMuPDF**, com Swagger, para corrigir a inclinacao e melhorar a legibilidade de PDFs digitalizados. Recebe um PDF e devolve outro PDF com as mesmas paginas e na mesma ordem, processadas individualmente.

## Executar no Windows (PowerShell)

Requer Python 3.11 ou superior. O ambiente local foi testado com Python 3.14.

```powershell terminal.ps1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Abra **http://127.0.0.1:8000/docs**. Expanda `POST /api/v1/pdfs/enhance`, clique em **Try it out**, selecione o PDF no campo `file`, execute e use o link de download da resposta.

- Swagger: `/docs`
- OpenAPI: `/openapi.json`
- Health check: `/health`

Tambem ha `POST /api/v1/pdfs/enhance-zip`: envie um ZIP e receba um ZIP com cada
PDF tratado no mesmo caminho interno. Arquivos que nao sao PDF permanecem no pacote
sem alteracao.

Para um PDF grande e misto, use `POST /api/v1/pdfs/enhance-and-group`. Ele trata
cada pagina, compara o layout e termos recorrentes e devolve um ZIP contendo
arquivos com nomes descritivos como `grupo-borderos-safra.pdf` quando houver termos
recorrentes confiaveis. Sem texto suficiente, usa `grupo-001.pdf`. Paginas sem
evidencia suficiente de um padrao repetido vao juntas para `padrao-nao-encontrado.pdf`.

### Tesseract: orientacao em 90/180/270 graus

A correcao de pequena inclinacao funciona sem Tesseract. Para detectar paginas de lado ou de cabeca para baixo, e necessario ter o executavel `tesseract` no `PATH`, com o modelo **osd.traineddata** disponivel. Use uma distribuicao confiavel ou um pacote portatil no seu usuario; nao e necessario configurar privilegios administrativos no projeto.

Para verificar uma instalacao existente:

```powershell verificar-tesseract.ps1
tesseract --version
tesseract --list-langs
```

A lista deve incluir `osd`. O pacote Python `pytesseract` **nao instala o executavel**. Sem ele, a API continua funcionando, mas informa `orientation_unavailable` no cabecalho de avisos. No Docker, o executavel e o modelo OSD ja estao incluidos. No Windows, a API tambem reconhece automaticamente a instalacao padrao em `C:\Program Files\Tesseract-OCR`, mesmo que o PATH do terminal ainda nao tenha sido atualizado.

Caso nao esteja instalado, no PowerShell use:

```powershell
winget install --id UB-Mannheim.TesseractOCR --exact
```

## Docker

Com Docker ja disponivel:

```powershell docker-exemplo.ps1
docker build -t endireita-pdf .
docker run --rm --name endireita-pdf -p 127.0.0.1:8000:8000 endireita-pdf
```

Para parar o container em outro terminal:

```powershell parar-docker.ps1
docker stop endireita-pdf
```

O Dockerfile executa a aplicacao como usuario nao root. A imagem Docker nao foi validada pelo teste local de Python.

## Chamada do endpoint

`POST /api/v1/pdfs/enhance`, corpo **multipart/form-data**. Todos os parametros sao campos do formulario, nao parametros de query. A resposta de sucesso e binaria, `application/pdf`, com nome `pdf-melhorado.pdf`.

Exemplo no Windows (use `curl.exe`, nao o alias `curl` do PowerShell antigo):

```powershell enviar-pdf.ps1
curl.exe --fail-with-body -X POST "http://127.0.0.1:8000/api/v1/pdfs/enhance" `
  -F "file=@C:/documentos/original.pdf;type=application/pdf" `
  -F "dpi=300" `
  -F "color_mode=grayscale" `
  -F "auto_orient=true" `
  --output "C:/documentos/melhorado.pdf"
```

Em caso de erro, a resposta e JSON com `detail` (e nao um PDF); o curl pode gravar esse JSON no caminho de saida. Verifique o status HTTP antes de usar o arquivo.

### Lote ZIP

`POST /api/v1/pdfs/enhance-zip` aceita os mesmos campos de processamento e devolve
`application/zip`. O ZIP e validado antes de extrair: caminhos fora do pacote, mais
de 200 arquivos ou mais de 500 MiB descompactados sao rejeitados por padrao.

```powershell enviar-lote.ps1
curl.exe --fail-with-body -X POST "http://127.0.0.1:8000/api/v1/pdfs/enhance-zip" `
  -F "file=@C:/documentos/entrada.zip;type=application/zip" `
  -F "auto_orient=true" `
  --output "C:/documentos/pdfs-melhorados.zip"
```

### Tratar e agrupar um PDF

```powershell agrupar-pdf.ps1
curl.exe --fail-with-body -X POST "http://127.0.0.1:8000/api/v1/pdfs/enhance-and-group" `
  -F "file=@C:/documentos/lote-misto.pdf;type=application/pdf" `
  -F "auto_orient=true" `
  --output "C:/documentos/pdfs-agrupados.zip"
```

O agrupamento e conservador: ele compara a distribuicao visual de texto, campos,
cabecalhos e elementos de pagina, e usa palavras OCR recorrentes como confirmacao e
para compor o nome curto de cada grupo.
Ele nao tenta inferir o assunto nem cria grupos de uma pagina; estas vao para
`padrao-nao-encontrado.pdf`. Os nomes dos grupos sao neutros porque o endpoint nao
atribui uma classificacao semantica que nao possa comprovar.

| Campo | Padrao | Descricao |
| --- | --- | --- |
| `file` | obrigatorio | Conteudo de um PDF valido, sem senha |
| `dpi` | `300` | Resolucao de renderizacao, entre 100 e 450 |
| `max_skew` | `15` | Limite da correcao de inclinacao em graus, entre 1 e 20 |
| `auto_orient` | `true` | Detectar rotacoes de 90/180/270 graus usando Tesseract OSD |
| `denoise` | `true` | Reducao moderada de ruido, preservando bordas |
| `remove_shadows` | `true` | Compensacao de iluminacao irregular |
| `sharpen` | `0.5` | Nitidez por unsharp mask, de 0 a 1.5; 0 desliga |
| `color_mode` | `grayscale` | `grayscale`, `color` ou `blackwhite` |

### Sugestoes de uso

- **Texto digitalizado:** comece com os parametros padrao.
- **Texto puro com fundo muito sujo:** experimente `color_mode=blackwhite`. Pode eliminar detalhes suaves, carimbos claros e imagens.
- **Carimbos, graficos e anotacoes coloridas:** use `color_mode=color`.
- **Fotografias ou paginas com grandes areas escuras:** use `color_mode=color`, `remove_shadows=false`, `sharpen=0.2`. O tratamento padrao e voltado a documentos com texto em papel claro, nao fotografias.
- **Halos nas letras ou texto muito fino:** reduza `sharpen`; compare tambem `denoise=false`.
- **PDF grande:** reduza `dpi` para 200 ou 150, ou divida o documento. Um DPI maior nao cria informacao ausente.

## Como funciona

1. Valida o corpo do upload, a assinatura do arquivo, o documento, a quantidade de paginas e o orcamento de pixels **antes de renderizar**.
2. Renderiza cada pagina no DPI solicitado, respeitando a orientacao de pagina ja armazenada no PDF.
3. Por padrao usa Tesseract OSD para detectar orientacao. Rotacoes de quadrante sao aplicadas com confianca moderada ou alta (limiar 5), para que scans de lado ou de cabeca para baixo sejam corrigidos sem forcar paginas com sinal insuficiente. OSD nao produz uma camada OCR no resultado.
4. Analisa uma versao reduzida em tons de cinza, remove margens e componentes de ruido e busca o melhor alinhamento por **perfil de projecao das linhas de texto**, primeiro em passos de 1 grau e depois de 0.1 grau. Exige ganho e pico suficientemente claros; a estimativa e heuristica, nao uma garantia de acerto. Uma busca interna mais ampla evita aplicar falsos picos quando a inclinacao excede `max_skew`.
5. Corrige a inclinacao com interpolacao bicubica e expande o canvas para **nao cortar as bordas da pagina**. Por isso, as dimensoes fisicas da pagina podem aumentar. Paginas reorientadas em 90/270 graus trocam largura e altura.
6. Compensa sombras estimando o fundo, aplica filtro bilateral, contraste local CLAHE e nitidez moderada. No modo colorido, trata a luminancia e preserva a crominancia.
7. Reconstroi o PDF com imagens PNG **sem perdas de compressao adicionais**. Preserva numero/ordem das paginas e alguns metadados descritivos, nao a estrutura original do PDF.

Nao utiliza IA generativa nem super-resolucao que possa inventar letras. Aumenta legibilidade quando o original permite, mas nao recupera detalhes perdidos por desfoque forte, baixa resolucao, compressao severa ou corte na digitalizacao. Nao corrige perspectiva de uma foto, curvatura de livros ou diferentes inclinacoes dentro da mesma pagina. Paginas sem linhas de texto suficientes podem permanecer sem correcao.

**Importante:** o PDF de saida e rasterizado. Texto selecionavel, camada OCR anterior, vetores, links, marcadores, anexos, formularios e assinaturas digitais nao sao preservados. O arquivo pode ficar maior. Para PDFs originalmente digitais e nitidos, o tratamento pode piorar a qualidade: esta API foi feita para scans. Mantenha o original e compare uma amostra antes de processar seu acervo inteiro.

## Estatisticas e avisos

Cabecalhos na resposta:

- `X-Pages-Processed`: paginas processadas.
- `X-Pages-Deskewed`: paginas cuja pequena inclinacao foi corrigida.
- `X-Pages-Reoriented`: paginas rotacionadas em 90/180/270 graus.
- `X-Processing-Warnings`: `none` ou codigos separados por virgula:
  - `orientation_unavailable`: Tesseract nao encontrado.
  - `orientation_undetermined`: OSD falhou, expirou ou nao havia texto suficiente/modelo disponivel.
  - `orientation_low_confidence`: orientacao nao aplicada por confianca insuficiente.

Os avisos sao agregados por documento; nao detalham a pagina. `X-Pages-Deskewed=0` pode significar que as paginas ja estavam alinhadas **ou** que a inclinacao nao foi estimada com seguranca.

Erros principais: `413` para limites excedidos, `422` para parametros/arquivo invalidos ou PDF com senha, `503` quando nao ha vaga apos a espera configurada, `500` para falha inesperada.

## Limites e operacao

Valores configuraveis por variaveis de ambiente (inteiros positivos):

| Variavel | Padrao |
| --- | --- |
| `MAX_UPLOAD_MB` | `50` |
| `MAX_PAGES` | `100` |
| `MAX_PAGE_MEGAPIXELS` | `20` |
| `MAX_TOTAL_MEGAPIXELS` | `300` |
| `MAX_CONCURRENT_JOBS` | `2` |
| `QUEUE_TIMEOUT_SECONDS` | `5` |
| `MAX_ZIP_FILES` | `200` |
| `MAX_ZIP_UNCOMPRESSED_MB` | `500` |

MB significa MiB (1024 x 1024 bytes). O corpo HTTP tem limite adicional de 1 MiB para o envelope multipart. Os limites de pixels incluem uma reserva para a expansao do canvas; 100 paginas nao necessariamente cabem no orcamento total a 300 DPI.

Exemplo de configuracao em uma maquina menor:

```powershell configurar.ps1
$env:MAX_CONCURRENT_JOBS = "1"
$env:MAX_TOTAL_MEGAPIXELS = "100"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

A CPU trabalha em **processos separados**, pois PyMuPDF nao suporta processamento concorrente em threads. O semaforo limita jobs ativos e evita uma fila de processamento ilimitada. Os limites sao **por processo web**: mantenha um worker Uvicorn inicialmente; varios workers multiplicam pools e consumo de memoria.

As imagens intermediarias sao liberadas por pagina, mas o PDF reconstruido acumula imagens em memoria ate ser salvo. O consumo depende de DPI, modo de cor e tamanho do documento. Para arquivos grandes com dois jobs, considere uma maquina com pelo menos 4 GB disponiveis e ajuste os limites com base em medicoes. A API e sincrona do ponto de vista HTTP: a conexao espera o processamento terminar; configure timeouts do cliente/proxy conforme seu acervo.

Nao envia PDFs a servicos externos. Arquivos temporarios sao removidos apos a resposta ou em erros normais; encerramento forcado do processo pode deixar temporarios no disco. Esta primeira versao nao tem autenticacao, rate limiting nem timeout global de CPU: **nao exponha diretamente a internet**. Em producao, adicione gateway/autenticacao, HTTPS, limites de conexao e recursos do container, limpeza de temporarios apos falhas e uma fila de jobs para documentos demorados. Limites de pixels nao garantem seguranca contra todo PDF malicioso. Avalie tambem a licenca AGPL/comercial do PyMuPDF antes de distribuir ou oferecer o servico.

## Testes

```powershell testar.ps1
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

Os testes cobrem inclinacoes sinteticas em ambos os sentidos, pagina em branco, deteccao de orientacao simulada, modos de imagem, upload/retorno de PDF, Swagger, documentos invalidos e limites. A deteccao real com o executavel Tesseract e a qualidade visual em PDFs do seu acervo precisam ser validadas com amostras reais.
