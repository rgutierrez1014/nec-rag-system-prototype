import os

from http_utils import http_get_with_retry, http_post_with_retry

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


def generate_embedding(text: str, prefix: str = "search_document") -> list[float]:
    response = http_post_with_retry(
        f"{OLLAMA_BASE_URL}/api/embeddings",
        timeout=30.0,
        json={"model": EMBEDDING_MODEL, "prompt": f"{prefix}: {text}"},
    )
    return response.json()["embedding"]


def get_embedding_model_version() -> str:
    response = http_get_with_retry(f"{OLLAMA_BASE_URL}/api/tags", timeout=10.0)
    for model in response.json().get("models", []):
        if model["name"].startswith(EMBEDDING_MODEL):
            return model.get("digest", EMBEDDING_MODEL)[:12]
    return EMBEDDING_MODEL
