    # Video Cuts Backend

Backend inicial para processar vídeos do YouTube e sugerir cortes.

## Funcionalidades iniciais
- Criar job via URL do YouTube
- Simular download de áudio
- Simular transcrição
- Listar segmentos com score inicial

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt