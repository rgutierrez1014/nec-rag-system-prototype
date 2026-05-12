import os

import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


def generate_embedding(text: str, prefix: str = "search_document") -> list[float]:
    response = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        json={"model": EMBEDDING_MODEL, "prompt": f"{prefix}: {text}"},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["embedding"]


def get_embedding_model_version() -> str:
    response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10.0)
    response.raise_for_status()
    for model in response.json().get("models", []):
        if model["name"].startswith(EMBEDDING_MODEL):
            return model.get("digest", EMBEDDING_MODEL)[:12]
    return EMBEDDING_MODEL
