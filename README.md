# Cut SaaS

Aplicacao local em FastAPI para baixar videos do YouTube, transcrever o audio, sugerir cortes e renderizar clips em formatos `short` e `long`.

## Estado atual

O projeto ja evoluiu de ferramenta local para uma base SaaS inicial. Hoje existem contas, sessoes, workspaces, isolamento de dados, migrations com Alembic, fila com worker separado, storage privado, URLs assinadas, usage events, quotas, billing inicial e telas web operacionais.

A suite local foi verificada com:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Resultado da ultima verificacao: `214 tests` passando.

O roadmap atualizado esta em `docs/saas-roadmap.md`. A prioridade atual e consolidar a base, validar staging com Postgres/API/worker separados, testar storage remoto e preparar um beta fechado com poucos usuarios.

## O que o projeto faz

- cria jobs a partir de uma URL do YouTube;
- permite upload local de videos;
- autentica usuarios e isola dados por workspace;
- baixa o video com `yt-dlp`;
- extrai o audio;
- transcreve com Whisper;
- detecta um nicho do conteudo;
- analisa a transcricao em chunks para videos longos;
- gera candidatos de forma incremental, permitindo primeiros resultados antes do fim da analise inteira;
- pontua os candidatos com heuristicas de gancho, clareza, fechamento, emocao e duracao;
- separa analise heuristica de enriquecimento por LLM;
- reranqueia candidatos com foco editorial para priorizar shorts mais enxutos, menos redundantes e com abertura mais forte;
- renderiza clips com `ffmpeg`;
- opcionalmente gera e queima legendas;
- oferece render manual mesmo sem concluir a analise;
- oferece interface web operacional com monitoramento, fila e endpoints HTTP;
- mede uso por workspace, aplica quotas e suporta billing inicial.

## Stack

- Python 3
- FastAPI
- SQLAlchemy
- Jinja2 templates
- SQLite local ou Postgres em staging/producao
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
  uploads/       videos enviados manualmente
docs/
  saas-roadmap.md roadmap tecnico e proximas etapas
video_cuts.db    banco SQLite local
```

## Fluxo do pipeline

Estados tipicos do job:

`pending -> downloading -> extracting_audio -> transcribing -> analyzing -> llm_enrichment -> done`

Estados adicionais usados na operacao:

- `cancel_requested`: cancelamento solicitado e aguardando checkpoint seguro;
- `canceled`: job encerrado manualmente;
- `failed`: erro com `error_message`;
- `pending` com mensagem de fila: aguardando slot de concorrencia.

Observacoes operacionais importantes:

- a etapa `analyzing` agora pode persistir candidatos por chunk em videos longos;
- a etapa `llm_enrichment` e opcional e pode ser pulada sem bloquear o job;
- jobs cancelados liberam slot para o proximo item da fila;
- a pagina do job mostra heartbeat, progresso percentual e sinais de possivel travamento.

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
ENVIRONMENT=local
DEBUG=False
DATABASE_URL=sqlite:///./video_cuts.db
BASE_DATA_DIR=./data
STORAGE_BACKEND=local
ARTIFACT_RETENTION_DAYS=30
PRESERVE_APPROVED_ARTIFACTS=True
SECRET_KEY=dev-secret-change-me
SESSION_COOKIE_SECURE=False
NODE_BIN=node
NODE_EXTRA_PATH=C:\Program Files\nodejs
YTDLP_VERBOSE=True
YTDLP_COOKIES_FILE=app/cookies/youtube_cookies.txt
WHISPER_MODEL=base
```

Use `.env.example` como base para criar o `.env` local. Em `staging` e `production`, o app valida configuracoes seguras no startup:

- `ENVIRONMENT` deve ser `staging` ou `production`;
- `DATABASE_URL` deve apontar para Postgres, preferencialmente `postgresql+psycopg://usuario:senha@host:5432/banco`;
- `SECRET_KEY` deve ser unico e ter pelo menos 32 caracteres;
- `DEBUG` deve ser `False`;
- em `production`, `SESSION_COOKIE_SECURE` deve ser `True`.

Variaveis relevantes:

- `ENVIRONMENT`: `local`, `test`, `staging` ou `production`
- `DATABASE_URL`: caminho do banco SQLite
- `BASE_DATA_DIR`: pasta base dos artefatos gerados
- `STORAGE_BACKEND`: `local`, `s3` ou `r2`; `local` usa `BASE_DATA_DIR`
- `STORAGE_BUCKET`: nome do bucket quando `STORAGE_BACKEND` for `s3` ou `r2`
- `STORAGE_PUBLIC_BASE_URL`: base publica opcional para objetos de storage
- `ARTIFACT_RETENTION_DAYS`: idade minima dos jobs para limpeza de artefatos
- `PRESERVE_APPROVED_ARTIFACTS`: preserva clips `ready`/`published` durante a limpeza
- `SECRET_KEY`: chave usada para assinar sessoes
- `SESSION_COOKIE_SECURE`: envia cookie de sessao apenas via HTTPS quando `True`
- `NODE_BIN`: binario do Node
- `NODE_EXTRA_PATH`: caminho adicional para encontrar o Node
- `YTDLP_COOKIES_FILE`: arquivo de cookies para o `yt-dlp`
- `YTDLP_COOKIES_BROWSER`: navegador para leitura de cookies
- `YTDLP_COOKIES_BROWSER_PROFILE`: perfil do navegador
- `YTDLP_VERBOSE`: logs detalhados do `yt-dlp`
- `WHISPER_MODEL`: modelo do Whisper, por exemplo `base`
- `LLM_TIMEOUT_SECONDS`: timeout das chamadas de enriquecimento por LLM
- `MAX_CONCURRENT_PIPELINE_JOBS`: limite de jobs pesados rodando ao mesmo tempo
- `PIPELINE_QUEUE_BACKEND`: `local` agenda pelo processo web; `worker` deixa jobs pendentes para `app.worker`

## Como rodar

Em desenvolvimento local, o app ainda cria/ajusta tabelas SQLite automaticamente para manter o ciclo curto:

```powershell
uvicorn app.main:app --reload
```

Depois abra:

- interface web: `http://127.0.0.1:8000/`
- healthcheck: `http://127.0.0.1:8000/health`

### Worker separado

Para manter o comportamento local mais simples, `PIPELINE_QUEUE_BACKEND=local` ainda agenda o pipeline via `BackgroundTasks`.
Para executar o processamento fora do servidor web, configure:

```env
PIPELINE_QUEUE_BACKEND=worker
```

Em um terminal, rode a API:

```powershell
uvicorn app.main:app --reload
```

Em outro terminal, rode o worker:

```powershell
python -m app.worker
```

Com esse backend, a API cria o job como `pending` e o worker consome a fila pelo banco. Para processar no maximo um job e encerrar, use:

```powershell
python -m app.worker --once
```

### Arquivos privados

Arquivos de usuario nao sao mais servidos diretamente por `/static`. A interface usa URLs temporarias em `/files/download/{token}`; o endpoint exige sessao autenticada e valida se o arquivo pertence ao workspace atual antes de devolver o conteudo.

### Planos e limites

O produto usa uma configuracao estatica inicial de planos em `app/services/plans.py`. Por enquanto todo workspace fica no plano `Free`, com limite mensal de 60 minutos de video processado.

Quando o workspace atinge o limite, novos jobs deixam de iniciar e a API retorna `402`. O dashboard exibe aviso a partir de 80% do consumo mensal. Arquivos ja gerados continuam disponiveis para download pelas URLs assinadas.

### Billing

A primeira integracao de billing usa um provider local `mock`, com o mesmo fluxo esperado para Stripe ou Mercado Pago: checkout, ativacao por retorno/webhook e atualizacao de assinatura.

Configure `BILLING_PROVIDER=mock` para o fluxo local. Para usar Stripe, configure `BILLING_PROVIDER=stripe`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` e o price do plano Starter em `STRIPE_PRICE_STARTER`. `mercado_pago` ja e um valor de configuracao validado, mas ainda exige adapter real antes de ativar checkout em producao.

- tela web: `/billing`
- status API: `GET /api/billing/status`
- checkout API: `POST /api/billing/checkout?plan=starter`
- webhook: `POST /api/billing/webhook`
- cancelamento: `POST /api/billing/cancel`

Assinaturas `active` ou `trialing` liberam o limite do plano contratado. Falha de pagamento marca a assinatura como `past_due`, fazendo o workspace voltar aos limites do plano `Free`. Cancelamentos marcam a assinatura como `canceled` e tambem retornam o workspace para o plano `Free`.

## Migrations

Em `staging` e `production`, aplique migrations antes de iniciar o servidor web:

```powershell
alembic upgrade head
```

Para criar uma nova migration depois de mudar modelos SQLAlchemy:

```powershell
alembic revision --autogenerate -m "descricao da mudanca"
```

O arquivo `alembic/env.py` usa o mesmo `DATABASE_URL` da aplicacao, incluindo a normalizacao para `postgresql+psycopg://`.

## Roadmap

O roadmap tecnico fica em `docs/saas-roadmap.md`.

Resumo das proximas etapas:

- consolidar o marco atual em commits revisaveis;
- validar staging com Postgres, Alembic, API e worker separado;
- testar storage remoto S3/R2 com arquivos privados;
- executar smoke tests com videos reais;
- preparar beta fechado com 3 a 5 usuarios e metricas de custo/qualidade.

## Como usar pela interface web

1. Abra a pagina inicial.
2. Envie uma URL do YouTube.
3. Aguarde o processamento do job.
4. Abra a pagina de detalhe do job.
5. Acompanhe pipeline, heartbeat, fila e progresso no monitoramento da pagina.
6. Revise os candidatos sugeridos, que podem aparecer de forma incremental durante `analyzing`.
7. Renderize um candidato ou informe tempos manualmente.

Atalhos uteis da interface:

- cancelar processamento;
- concluir analise sem LLM;
- reprocessar etapa especifica;
- render manual imediato mesmo sem transcricao finalizada.

## Rotas principais da API

### Infra

- `GET /health`
- `GET /jobs/debug/node`

### Jobs

- `POST /jobs/youtube`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/monitor`
- `POST /jobs/{job_id}/analyze`
- `POST /jobs/{job_id}/cancel`
- `GET /jobs/{job_id}/candidates`
- `GET /jobs/{job_id}/approved-candidates`
- `GET /jobs/{job_id}/clips`
- `GET /jobs/health/pipeline`
- `GET /jobs/dashboard/monitor`

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
- o painel web foi desenhado para operacao local, com polling parcial e foco em recuperar jobs longos;
- para videos longos, a analise incremental por chunks reduz o tempo ate o primeiro candidato;
- para beta/staging, a proxima validacao importante e rodar API e worker separados com Postgres;
- para producao, ainda vale adicionar uma fila externa mais robusta e observabilidade estruturada.
