# Cut SaaS

Aplicacao local em FastAPI para baixar videos do YouTube, transcrever o audio, sugerir cortes e renderizar clips em formatos `short` e `long`.

## O que o projeto faz

- cria jobs a partir de uma URL do YouTube;
- baixa o video com `yt-dlp`;
- extrai o audio;
- transcreve com Whisper;
- detecta um nicho do conteudo;
- gera janelas candidatas para cortes;
- pontua os candidatos com heuristicas de gancho, clareza, fechamento, emocao e duracao;
- reranqueia candidatos com foco editorial para priorizar shorts mais enxutos, menos redundantes e com abertura mais forte;
- renderiza clips com `ffmpeg`;
- opcionalmente gera e queima legendas;
- oferece interface web simples e endpoints HTTP.

## Stack

- Python 3
- FastAPI
- SQLAlchemy
- Jinja2 templates
- SQLite
- `yt-dlp`
- `openai-whisper`
- `ffmpeg`

## Estrutura principal

```text
app/
  api/           rotas HTTP da API
  core/          configuracoes
  db/            conexao com banco
  models/        modelos SQLAlchemy
  schemas/       payloads Pydantic
  services/      pipeline e regras de negocio
  templates/     interface web server-rendered
  utils/         utilitarios de arquivos, URLs e ambiente
data/
  downloads/     videos e audios baixados
  transcripts/   transcricoes em JSON
  clips/         clips renderizados
  subtitles/     legendas geradas
video_cuts.db    banco SQLite local
```

## Fluxo do pipeline

Estados tipicos do job:

`pending -> downloading -> extracting_audio -> transcribing -> analyzing -> done`

Em caso de falha, o job vai para `failed` e registra `error_message`.

## Qualidade editorial da analise

A etapa de analise dos candidatos combina segmentacao com heuristicas editoriais para melhorar a qualidade dos cortes sugeridos.

Hoje o ranking considera especialmente:

- aderencia de duracao ao formato `short` ou `long`;
- forca da abertura, distinguindo gancho real de abertura apenas informativa;
- clareza de inicio e fechamento;
- dependencia de contexto anterior, para evitar trechos que nao se sustentam sozinhos;
- diversidade entre candidatos, reduzindo cortes muito parecidos entre si;
- sinais de impacto, estrutura e densidade informacional.

Na pratica, isso ajuda o sistema a:

- reduzir cortes longos demais para `short`;
- evitar repeticao de candidatos com a mesma abertura;
- subir trechos com tensao, promessa, pergunta ou contraste logo no inicio;
- filtrar melhor segmentos que dependem demais do contexto anterior.

## Requisitos

Antes de rodar, tenha instalado:

- Python 3.11+ recomendado
- `ffmpeg` disponivel no `PATH`
- Node.js disponivel no `PATH` ou configurado via `NODE_BIN` / `NODE_EXTRA_PATH`

Observacao:

- o download do YouTube neste projeto valida a disponibilidade do Node antes de chamar o `yt-dlp`;
- dependendo do video, cookies do navegador ou arquivo de cookies podem ser necessarios.

## Instalacao

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Linux/macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuracao

O projeto usa variaveis de ambiente via `.env`.

Exemplo minimo:

```env
APP_NAME=Video Cuts Backend
DEBUG=True
DATABASE_URL=sqlite:///./video_cuts.db
BASE_DATA_DIR=./data
NODE_BIN=node
NODE_EXTRA_PATH=C:\Program Files\nodejs
YTDLP_VERBOSE=True
YTDLP_COOKIES_FILE=app/cookies/youtube_cookies.txt
WHISPER_MODEL=base
```

Variaveis relevantes:

- `DATABASE_URL`: caminho do banco SQLite
- `BASE_DATA_DIR`: pasta base dos artefatos gerados
- `NODE_BIN`: binario do Node
- `NODE_EXTRA_PATH`: caminho adicional para encontrar o Node
- `YTDLP_COOKIES_FILE`: arquivo de cookies para o `yt-dlp`
- `YTDLP_COOKIES_BROWSER`: navegador para leitura de cookies
- `YTDLP_COOKIES_BROWSER_PROFILE`: perfil do navegador
- `YTDLP_VERBOSE`: logs detalhados do `yt-dlp`
- `WHISPER_MODEL`: modelo do Whisper, por exemplo `base`

## Como rodar

```powershell
uvicorn app.main:app --reload
```

Depois abra:

- interface web: `http://127.0.0.1:8000/`
- healthcheck: `http://127.0.0.1:8000/health`

## Como usar pela interface web

1. Abra a pagina inicial.
2. Envie uma URL do YouTube.
3. Aguarde o processamento do job.
4. Abra a pagina de detalhe do job.
5. Revise os candidatos sugeridos.
6. Renderize um candidato ou informe tempos manualmente.

## Rotas principais da API

### Infra

- `GET /health`
- `GET /jobs/debug/node`

### Jobs

- `POST /jobs/youtube`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/analyze`
- `GET /jobs/{job_id}/candidates`
- `GET /jobs/{job_id}/approved-candidates`
- `GET /jobs/{job_id}/clips`

### Renderizacao

- `POST /jobs/{job_id}/render`
- `POST /jobs/{job_id}/render-candidate`
- `POST /jobs/{job_id}/render-candidate-id/{candidate_id}`
- `POST /jobs/{job_id}/render-approved`
- `POST /jobs/{job_id}/render-manual`

### Moderacao de candidatos

- `POST /jobs/candidates/{candidate_id}/approve`
- `POST /jobs/candidates/{candidate_id}/reject`
- `POST /jobs/candidates/{candidate_id}/reset`

### Nichos

- `POST /jobs/niches/{niche}/learn-keywords`
- `GET /jobs/niches/{niche}/keywords`

## Exemplo rapido de uso da API

Criar job:

```bash
curl -X POST "http://127.0.0.1:8000/jobs/youtube" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://www.youtube.com/watch?v=VIDEO_ID\"}"
```

Analisar candidatos:

```bash
curl -X POST "http://127.0.0.1:8000/jobs/1/analyze" \
  -H "Content-Type: application/json" \
  -d "{\"mode\":\"short\",\"top_n\":10}"
```

Renderizar um candidato:

```bash
curl -X POST "http://127.0.0.1:8000/jobs/1/render-candidate" \
  -H "Content-Type: application/json" \
  -d "{\"candidate_index\":0,\"burn_subtitles\":true,\"mode\":\"short\"}"
```

## Como os dados sao salvos

- o banco `video_cuts.db` guarda jobs, candidatos, clips e palavras-chave de nicho;
- a pasta `data/downloads` guarda o material original baixado;
- a pasta `data/transcripts` guarda os JSONs da transcricao;
- a pasta `data/clips/job_<id>` guarda os videos renderizados;
- a pasta `data/subtitles/job_<id>` guarda os arquivos `.ass`.

## Observacoes de desenvolvimento

- as tabelas sao criadas automaticamente ao iniciar a aplicacao;
- as pastas base de dados sao garantidas no startup;
- ha alteracoes locais nao commitadas no repositorio, entao revise o estado do git antes de empacotar ou publicar;
- para producao, vale adicionar migracoes, testes automatizados e tratamento mais robusto para downloads e filas.
