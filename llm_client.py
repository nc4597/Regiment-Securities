import os
import time
import json
import urllib.request
from typing import Optional

DEFAULT_MODEL = "mistral"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.2
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.5
OLLAMA_TIMEOUT_SECONDS = 600
OLLAMA_NUM_CTX = 24576

# Provider endpoints (no API key needed for local providers)
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
JAN_ENDPOINT = "http://localhost:1337/v1/chat/completions"
GPT4ALL_ENDPOINT = "http://localhost:4891/v1/chat/completions"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"


def _detect_provider() -> str:
    """
    Detect which LLM provider is available by trying to connect to each endpoint.
    Returns provider name or raises error if none available.
    """
    forced_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    
    providers_to_try = []
    if forced_provider:
        providers_to_try = [forced_provider]
    else:
        providers_to_try = ["openai", "ollama", "jan", "gpt4all"]
    
    for provider in providers_to_try:
        if provider == "openai":
            if os.getenv("OPENAI_API_KEY", "").strip():
                return provider
            continue

        endpoint = {
            "ollama": OLLAMA_ENDPOINT,
            "jan": JAN_ENDPOINT,
            "gpt4all": GPT4ALL_ENDPOINT,
        }.get(provider)
        
        if not endpoint:
            continue
        
        try:
            req = urllib.request.Request(endpoint, method="OPTIONS")
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status < 400:
                    return provider
        except Exception:
            pass
    
    if forced_provider:
        raise RuntimeError(
            f"LLM provider '{forced_provider}' is not available. "
            f"Please start {forced_provider} or install it."
        )
    
    raise RuntimeError(
        "No LLM provider detected. Please start one of:\n"
        "  - Ollama: https://ollama.ai\n"
        "  - Jan: https://jan.ai\n"
        "  - GPT4All: https://www.nomic.ai/gpt4all\n"
        "Or set OPENAI_API_KEY for hosted OpenAI-compatible generation.\n"
        "You can also set LLM_PROVIDER to force a specific provider."
    )


def generate_text(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """
    Generate text using the first available LLM provider (Ollama, Jan, or GPT4All).
    Falls back to next provider if one is unavailable.
    Set LLM_PROVIDER env var to force a specific provider.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer.")

    provider = _detect_provider()

    for attempt in range(1, retries + 1):
        try:
            if provider == "ollama":
                return _generate_ollama(prompt, model, max_tokens, temperature)
            elif provider == "jan":
                return _generate_jan(prompt, model, max_tokens, temperature)
            elif provider == "gpt4all":
                return _generate_gpt4all(prompt, model, max_tokens, temperature)
            elif provider == "openai":
                openai_model = model
                if openai_model == DEFAULT_MODEL:
                    openai_model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
                return _generate_openai(openai_model, prompt, max_tokens, temperature)
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f"LLM request failed after {retries} attempts: {exc}") from exc
            time.sleep(DEFAULT_BACKOFF_SECONDS * attempt)

    raise RuntimeError("LLM request failed unexpectedly.")


def _generate_ollama(
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Generate text using Ollama."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    req = urllib.request.Request(
        OLLAMA_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
        return data.get("response", "").strip()


def _generate_jan(
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Generate text using Jan (OpenAI-compatible endpoint)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 1.0,
    }

    req = urllib.request.Request(
        JAN_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""


def _generate_gpt4all(
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Generate text using GPT4All (OpenAI-compatible endpoint)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 1.0,
    }

    req = urllib.request.Request(
        GPT4ALL_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""


def _generate_openai(
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Generate text using OpenAI's hosted OpenAI-compatible endpoint."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when using the openai provider.")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    req = urllib.request.Request(
        OPENAI_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""
