# Roadmap Tecnico para SaaS Comercial

Atualizado em 2026-04-27.

Este roadmap agora reflete o estado real do repositorio. A base SaaS inicial ja saiu do papel: o produto tem contas, workspaces, isolamento, migrations, fila/worker, storage privado, quotas, billing inicial e fluxo web operacional. A prioridade deixou de ser criar a fundacao e passou a ser estabilizar staging, validar processamento real e preparar beta fechado.

## Norte do Produto

O sistema deve permitir que criadores, editores ou agencias enviem videos, recebam sugestoes de cortes, revisem candidatos, renderizem clips e exportem/publicem o material com confiabilidade.

O diferencial principal nao e apenas "cortar video"; e reduzir o tempo entre video bruto e cortes publicaveis com qualidade editorial consistente.

## Estado Atual

Base validada localmente:

- contas, cadastro, login, logout e sessoes por cookie;
- modelos `User`, `Workspace` e `WorkspaceMember`;
- isolamento por workspace em jobs, candidatos, clips, nichos, feedback editorial e arquivos;
- configuracao por ambiente com validacoes para staging/producao;
- Alembic configurado com migrations iniciais;
- suite de testes local cobrindo auth, isolamento, rotas, fila, worker, storage, billing, quotas, retencao e pipeline;
- abstracao de fila com backend local e modo `worker`;
- worker separado em `app.worker`;
- locks, retry e recuperacao de jobs interrompidos;
- storage local abstraido com preparacao para S3/R2;
- URLs assinadas para arquivos privados;
- retencao e calculo de storage por workspace;
- usage events para video processado, renders, LLM e storage;
- planos estaticos, quotas mensais e bloqueio de novos jobs acima do limite;
- billing inicial com provider `mock` e adapter Stripe;
- onboarding, dashboard, tela de billing e fluxo editorial server-rendered;
- pacote de export com metadados editoriais/publicacao.

Ultima verificacao local:

- comando: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`
- resultado: `214 tests` passando.

## Principios de Execucao

- Manter PRs pequenos e reversiveis.
- Proteger dados por workspace antes de adicionar novas features.
- Medir custo por job antes de abrir uso pago.
- Tratar processamento pesado como workflow duravel, nao como request web.
- Preferir um SaaS simples e confiavel a uma ferramenta cheia de opcoes instaveis.
- Criar testes de isolamento, permissao e retry para cada mudanca estrutural.

## Marcos Entregues

### Marco 1: Base SaaS Minima

Status: entregue localmente.

Entregas:

- modelos de conta, workspace e membership;
- usuario owner inicial do workspace;
- cadastro, login e logout;
- sessoes HTTP-only;
- paginas principais protegidas;
- isolamento de jobs, candidatos, clips e exports;
- nichos globais e customizados por workspace;
- feedback editorial isolado por workspace.

Evidencias:

- `app/models/user.py`
- `app/models/workspace.py`
- `app/models/workspace_member.py`
- `app/services/auth.py`
- `app/services/accounts.py`
- `app/api/deps.py`
- `tests/test_auth.py`
- `tests/test_accounts.py`
- `tests/test_workspace_isolation.py`
- `tests/test_niche_workspace.py`

### Marco 2: Producao Confiavel

Status: implementado, pendente de ensaio real em staging.

Entregas:

- configuracao por ambiente;
- exigencia de Postgres em staging/producao;
- validacao de `SECRET_KEY`, `DEBUG` e cookie seguro;
- Alembic com migrations iniciais;
- CI rodando `unittest discover`;
- suite local estavel.

Evidencias:

- `app/core/config.py`
- `app/db/database.py`
- `alembic.ini`
- `alembic/versions/`
- `.github/workflows/ci.yml`
- `tests/test_config.py`
- `tests/test_alembic_setup.py`
- `tests/test_migrations.py`

### Marco 3: Fila, Workers e Processamento Duravel

Status: implementado localmente, pendente de carga real.

Entregas:

- `enqueue_pipeline_job`;
- backend local via `BackgroundTasks`;
- backend `worker` que deixa jobs pendentes para consumo externo;
- comando `python -m app.worker`;
- lock por job;
- retry por etapa;
- recuperacao de jobs travados;
- cancelamento seguro.

Evidencias:

- `app/services/queue.py`
- `app/worker.py`
- `app/services/pipeline.py`
- `tests/test_queue.py`
- `tests/test_worker.py`
- `tests/test_pipeline_recovery.py`

### Marco 4: Storage Privado e Retencao

Status: implementado em base local, pendente de validacao S3/R2.

Entregas:

- servico de storage;
- backend local;
- configuracao para S3/R2;
- URLs assinadas para arquivos privados;
- endpoint autenticado de download;
- verificacao de workspace antes de liberar arquivo;
- limpeza de artefatos expirados;
- preservacao de clips aprovados/publicados conforme politica.

Evidencias:

- `app/services/storage.py`
- `app/api/routes_files.py`
- `app/utils/media_urls.py`
- `app/services/retention.py`
- `tests/test_storage.py`
- `tests/test_file_access.py`
- `tests/test_retention.py`

### Marco 5: Billing, Planos e Quotas

Status: implementado para beta com `mock`/Stripe teste, pendente de decisao comercial final.

Entregas:

- usage events;
- plano `Free` e plano pago inicial;
- limite mensal por workspace;
- bloqueio de novos jobs acima da quota;
- tela de billing;
- checkout mock;
- adapter Stripe;
- webhooks de billing;
- fallback para plano Free em falha/cancelamento.

Evidencias:

- `app/models/usage_event.py`
- `app/models/subscription.py`
- `app/services/usage.py`
- `app/services/plans.py`
- `app/services/quota.py`
- `app/services/billing.py`
- `app/api/routes_billing.py`
- `app/web/routes_billing.py`
- `tests/test_usage.py`
- `tests/test_quota.py`
- `tests/test_billing.py`

### Marco 6: UX Vendavel

Status: parcialmente entregue.

Entregas:

- onboarding para primeiro job;
- dashboard operacional;
- monitoramento de fila e pipeline;
- revisao de candidatos;
- aprovacao/rejeicao/reset;
- render manual;
- render de candidatos aprovados;
- status de publicacao por clip;
- export com metadados.

Pendente:

- polimento visual do fluxo completo;
- teste mobile/desktop no navegador;
- copy menos tecnica para usuario nao desenvolvedor;
- revisao de estados vazios, erros e carregamento;
- comparacao melhor entre candidatos similares.

## Proximas Etapas

### 1. Consolidar o marco atual

Objetivo: transformar a base atual em uma entrega revisavel.

Tarefas:

- organizar o working tree em commits ou PRs coerentes;
- evitar incluir artefatos locais, bancos e cookies reais em commits;
- atualizar documentacao de setup, staging e beta;
- revisar `.gitignore` para `data/`, bancos locais, caches e cookies sensiveis;
- garantir que `README.md` e este roadmap descrevam o produto atual.

Criterio de aceite:

- branch limpa depois do commit;
- documentacao corresponde ao codigo;
- nenhuma credencial, cookie real ou artefato pesado versionado.

### 2. Ensaio de staging

Objetivo: provar que o SaaS sobe fora do ambiente local.

Tarefas:

- criar banco Postgres de staging;
- configurar `ENVIRONMENT=staging`;
- configurar `DATABASE_URL`, `SECRET_KEY`, `SESSION_COOKIE_SECURE` e `DEBUG=False`;
- rodar `alembic upgrade head`;
- iniciar API e worker separados;
- validar login, upload/URL, processamento, render e download assinado.

Criterio de aceite:

- app inicia em staging sem `create_all` como dependencia;
- worker processa job pendente apos restart da API;
- arquivos privados continuam protegidos;
- logs permitem diagnosticar falhas por etapa.

### 3. Validar storage remoto

Objetivo: sair do disco local para artefatos de usuario em staging.

Tarefas:

- configurar bucket privado S3 ou R2;
- validar escrita/leitura de downloads, transcripts, clips, subtitles e exports;
- validar URLs assinadas;
- testar bloqueio de acesso cruzado por workspace;
- rodar limpeza de retencao em ambiente controlado.

Criterio de aceite:

- usuario ve e baixa apenas seus arquivos;
- limpeza remove somente artefatos expirados e preserva clips protegidos;
- storage usado por workspace e calculado corretamente.

### 4. Smoke test com videos reais

Objetivo: medir confiabilidade e custo do pipeline com conteudo real.

Tarefas:

- processar videos curtos, medios e longos;
- testar YouTube com e sem cookies;
- testar upload local;
- medir tempo ate primeiro candidato;
- medir tempo ate primeiro clip;
- registrar falhas por etapa;
- comparar qualidade dos candidatos aprovados.

Criterio de aceite:

- pelo menos 10 jobs reais processados em staging;
- taxa de jobs com ao menos 1 clip util registrada;
- principais falhas conhecidas documentadas.

### 5. Preparar beta fechado

Objetivo: abrir para poucos usuarios sem perder controle operacional.

Tarefas:

- escolher provider de billing para beta: `mock` acompanhado ou Stripe teste/real;
- definir limite Free e Starter;
- criar checklist de suporte manual;
- preparar template de feedback;
- acompanhar 3 a 5 usuarios por 2 semanas;
- medir custo medio por minuto processado.

Criterio de aceite:

- beta tem usuarios, limite, forma de suporte e metricas claras;
- nenhum dado cru de um workspace aparece para outro;
- falhas de pipeline sao recuperaveis ou explicaveis.

## Ordem Recomendada dos Proximos 10 PRs

1. Atualizar documentacao, roadmap, `.gitignore` e higiene de artefatos locais.
2. Criar guia de deploy/staging com Postgres, Alembic, API e worker.
3. Validar staging com Postgres e registrar checklist operacional.
4. Integrar e testar S3/R2 em staging.
5. Adicionar smoke tests manuais documentados para videos reais.
6. Melhorar observabilidade operacional: logs estruturados, ids de job/workspace e resumo de falhas.
7. Polir UX de onboarding, dashboard, job detail e billing para beta.
8. Fechar decisao de billing beta e fluxo de assinatura.
9. Criar painel/admin minimo para acompanhar jobs, custos e falhas.
10. Rodar beta fechado e transformar feedback real em ajustes editoriais.

## Beta Fechado

Entrar em beta somente depois destes itens:

- staging com Postgres validado;
- API e worker separados validados;
- storage privado remoto validado;
- limites basicos por workspace funcionando;
- billing beta definido;
- testes principais passando;
- smoke test com videos reais concluido;
- custo por job/minuto medido.

Perfil recomendado:

- 3 a 5 usuarios;
- criadores/agencias com conteudo proprio;
- uso acompanhado por 2 semanas;
- feedback focado em tempo economizado e qualidade dos cortes.

Metricas:

- tempo ate primeiro candidato;
- tempo ate primeiro clip renderizado;
- percentual de candidatos aprovados;
- percentual de jobs com pelo menos 1 clip util;
- custo medio por minuto processado;
- falhas por etapa do pipeline;
- taxa de reprocessamento;
- storage medio por workspace;
- renders por job.

## Backlog Depois do Beta

- publicacao direta em plataformas;
- templates de marca por workspace;
- convite de membros por equipe;
- permissoes por papel: owner, editor, viewer;
- fila prioritaria por plano;
- dashboard financeiro interno;
- auditoria de acoes;
- API publica com tokens;
- webhooks para clientes;
- melhorias no motor editorial com base nos feedbacks reais.

## Primeira Tarefa Recomendada Agora

Comecar pela consolidacao do marco atual.

Escopo exato:

- atualizar roadmap e README;
- revisar o que deve ou nao entrar no commit;
- garantir `.gitignore` para artefatos sensiveis/locais;
- commitar uma base documentada;
- em seguida preparar o ensaio de staging.

Motivo:

O codigo ja passou da fase de fundacao. O risco principal agora nao e falta de feature, e sim transformar um conjunto grande de mudancas em uma base rastreavel, implantavel e segura para usuarios reais.
