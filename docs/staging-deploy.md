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

## 9. Limite atual do projeto

Hoje o projeto ainda nao possui implementacao real de storage remoto, apesar de aceitar os valores `s3` e `r2` na configuracao.

Na pratica:

- `Stripe` ja pode ser preparado para staging
- `S3/R2` ainda nao devem ser ativados em deploy real sem implementar um backend alem de `LocalStorage`

## 10. Proximos upgrades recomendados

- trocar `STORAGE_BACKEND=local` por `s3` ou `r2`
- trocar `BILLING_PROVIDER=mock` por `stripe`
- adicionar CSRF, rate limit e headers de seguranca
