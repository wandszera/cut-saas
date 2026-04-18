import json
import time

import httpx

from app.core.config import settings


class LLMRateLimitError(RuntimeError):
    pass


def generate_json_with_llm(prompt: str, *, timeout: float = 45.0) -> dict | list:
    provider = (settings.llm_provider or "").strip().lower()

    if provider == "ollama":
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.llm_model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload = response.json()

        raw_output = (payload.get("response") or "").strip()
        try:
            return json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Resposta do Ollama não veio em JSON válido: {raw_output}") from exc

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY não configurada")

        payload = None
        last_rate_limit_error = None
        for attempt in range(3):
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.llm_model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": "Responda apenas com JSON válido, sem markdown.",
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                    },
                )

            if response.status_code == 429:
                last_rate_limit_error = LLMRateLimitError(
                    "OpenAI retornou 429 Too Many Requests"
                )
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise last_rate_limit_error

            response.raise_for_status()
            payload = response.json()
            break

        if payload is None:
            raise RuntimeError("A OpenAI não retornou payload utilizável")

        try:
            raw_output = payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError("Resposta da OpenAI veio sem conteúdo utilizável") from exc

        try:
            return json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Resposta da OpenAI não veio em JSON válido: {raw_output}") from exc

    raise RuntimeError(f"Provider de LLM não suportado: {settings.llm_provider}")
