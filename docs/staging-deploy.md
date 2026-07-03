# Staging Deploy

Este guia resume o caminho mais curto para subir uma versao de teste com API e worker separados.

## 1. Preparar o ambiente

Use o arquivo [`.env.staging.example`](C:/Users/wand/Desktop/cut_saas/.env.staging.example) como base:

```powershell
Copy-Item .env.staging.example .env
```

Preencha pelo menos:

- `DATABASE_URL`
- `SECRET_KEY`
- `SESSION_COOKIE_SECURE=True`
- `ALLOWED_HOSTS`
- `PROXY_TRUSTED_HOSTS`
- `PIPELINE_QUEUE_BACKEND=worker`
- `BILLING_PROVIDER`

Se quiser um exemplo pronto para Stripe, use [`.env.staging.stripe.example`](C:/Users/wand/Desktop/cut_saas/.env.staging.stripe.example).
Se quiser fechar o ultimo passo do storage remoto em Cloudflare R2, use [`.env.staging.r2.example`](C:/Users/wand/Desktop/cut_saas/.env.staging.r2.example).

## 2. Instalar dependencias

Para staging focado em performance de transcricao, prefira montar o ambiente com `Python 3.11` ou `3.12`:

```powershell
.\scripts\Setup-StagingRuntime.ps1 -PrintOnly
```

Se o script encontrar um `Python 3.11/3.12`, ele pode montar o virtualenv e instalar as dependencias automaticamente:

```powershell
.\scripts\Setup-StagingRuntime.ps1
```

Se voce quiser forcar uma versao especifica:

```powershell
.\scripts\Setup-StagingRuntime.ps1 -PythonVersion 3.12
```

Se a maquina ainda nao tiver `Python 3.11` ou `3.12`, voce pode preparar o instalador recomendado:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-RecommendedPython.ps1 -PrintOnly
```

Para instalar de fato via `winget`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-RecommendedPython.ps1
```

Fluxo manual equivalente:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Observacao importante sobre transcricao:

- `faster-whisper` tende a ser a opcao mais rapida para staging, mas no ambiente atual ele nao instala em `Python 3.13`
- para usar `TRANSCRIPTION_PROVIDER=auto` com `faster-whisper` de verdade, prefira `Python 3.11` ou `3.12`
- se voce permanecer em `Python 3.13`, o sistema deve cair para `openai_whisper`, e o diagnostics vai mostrar esse fallback

Valide os binarios necessarios:

```powershell
ffmpeg -version
ffprobe -version
node -v
```

## 3. Validar readiness

```powershell
.\scripts\Invoke-StagingReadiness.ps1 -EnvFile .env.staging.local
```

O comando precisa sair com todos os checks relevantes resolvidos antes da subida.
Se existir um ambiente `.\.venv312`, os scripts de staging passam a preferi-lo automaticamente.

Agora o readiness tambem valida o runtime de transcricao.
Se `TRANSCRIPTION_PROVIDER` estiver em `auto` ou `faster_whisper`, o check `Transcription runtime` so fica `ok` quando:

- o ambiente estiver em `Python 3.11` ou `3.12`
- `faster-whisper` estiver instalado no virtualenv

## 4. Aplicar migrations

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

## 5. Subir a API

```powershell
.\scripts\Start-StagingApi.ps1 -EnvFile .env.staging.local
```

## 6. Subir o worker

Em outro terminal:

```powershell
.\scripts\Start-StagingWorker.ps1 -EnvFile .env.staging.local
```

Se quiser revisar os comandos antes de subir:

```powershell
.\scripts\Start-StagingApi.ps1 -EnvFile .env.staging.local -PrintOnly
.\scripts\Start-StagingWorker.ps1 -EnvFile .env.staging.local -PrintOnly
```

## 7. Smoke test obrigatorio

Depois que API e worker estiverem no ar:

1. Acessar `/health`
2. Criar conta
3. Fazer login
4. Enviar 1 video de ate 30 minutos
5. Confirmar processamento do worker
6. Confirmar candidatos de trial:
   - no maximo `10` shorts
   - no maximo `3` longos
7. Confirmar render e download
8. Confirmar bloqueio do segundo video sem billing

## 8. Estado minimo aceitavel para staging

- Postgres em uso
- Alembic aplicado
- API e worker separados
- HTTPS no dominio de teste
- `SECRET_KEY` forte
- `ALLOWED_HOSTS` apontando para o dominio real do staging
- `PROXY_TRUSTED_HOSTS` alinhado com o proxy/reverse proxy usado
- sem arquivo local de cookies do YouTube no deploy

## 9. Storage remoto

O projeto agora possui backend remoto para `s3` e `r2`, com cache local e sincronizacao dos artefatos principais do pipeline.

Na pratica:

- `Stripe` ja pode ser preparado para staging
- `S3/R2` ja podem ser preparados para staging
- para `R2`, preencha:
  - `STORAGE_BACKEND=r2`
  - `STORAGE_BUCKET`
  - `STORAGE_ENDPOINT_URL`
  - `STORAGE_REGION=auto`
  - `STORAGE_ACCESS_KEY_ID`
  - `STORAGE_SECRET_ACCESS_KEY`
- para `S3`, preencha:
  - `STORAGE_BACKEND=s3`
  - `STORAGE_BUCKET`
  - `STORAGE_REGION`
  - `STORAGE_ACCESS_KEY_ID`
  - `STORAGE_SECRET_ACCESS_KEY`

Quando essas variaveis estiverem preenchidas, o readiness deve sair de `8/9` para `9/9`.

## 10. Proximos upgrades recomendados

- preencher credenciais reais de `s3` ou `r2`
- rodar `alembic upgrade head`
- subir API e worker com o runtime `.\.venv312`
- executar smoke test completo com upload, render e download

## 11. Checklist Operacional de Faturamento & Cloud (Fase Comercial)

Para testar as melhorias comerciais de faturamento entregues, siga o roteiro de testes:

### A. Validação de Assinaturas & Quotas
1. **Ativação de Trial**: Configure `TRIAL_DAYS=7` no `.env` e assinale um plano (Starter ou Pro) -> Garanta que o status da assinatura mude para `Em periodo de teste (trialing)`.
2. **Alertas Automáticos de Cotas**:
   - Processe um vídeo longo para ultrapassar 80% do plano contratado.
   - Monitore a saída de logs para checar o disparo do e-mail transacional de 80% (`check_and_send_quota_warnings`).
   - Tente processar um segundo vídeo longo para atingir 100% da cota mensal -> Verifique o bloqueio com `HTTP 402` e o e-mail de 100% disparado.
3. **Expiração On-the-Fly**: No banco de dados, force a coluna `current_period_end` da assinatura ativa para o passado -> Recarregue a aba `/billing` -> Certifique-se de que a assinatura foi cancelada automaticamente e o workspace rebaixado graciosamente para o plano Free.

### B. Integração Mercado Pago (Pix/Boleto)
1. **Mock Gateway**: Defina `BILLING_PROVIDER=mercado_pago` sem chaves de acesso -> Faça um checkout -> O sistema deve usar o adapter em modo Sandbox criando a assinatura `mp_pre_...` e permitindo completar sem cartão.
2. **Gateway Real**: Defina `MERCADO_PAGO_ACCESS_TOKEN` e configure as credenciais -> Simule checkout Pix e pre-approval -> O adapter deve bater na API oficial e devolver o link do checkout nativo do Mercado Pago.

### C. Dashboard Administrativo Geral
1. **Acesso Protegido**: Faça login com um usuário comum e tente forçar a URL `/admin` -> O sistema deve retornar `HTTP 403 (Acesso restrito)`.
2. **Painel do Administrador**: Marque o usuário no banco de dados como admin (`is_admin = True`) -> Acesse `/admin` -> Verifique o cálculo de MRR, Churn, custos médios por minuto processado (Whisper + LLMs) e a lista dinâmica contendo todos os workspaces e seus relatórios de consumo.
3. **Navegação Integrada**: Garanta que o botão "Painel Admin" apareça automaticamente no menu lateral e possua borda estática em acid green.

