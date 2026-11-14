"""Download embedding and reranker models at Docker build time.

Reads model names from Settings so they stay in sync with the app.
HF_HOME must be set before running so files land in the image cache.
"""

from __future__ import annotations

from sentence_transformers import CrossEncoder, SentenceTransformer

from recall_assistant.config import Settings


def main() -> None:
    settings = Settings()
    print(f"Downloading embedding model: {settings.embedding_model}")
    SentenceTransformer(settings.embedding_model, device="cpu")
    print(f"Downloading reranker model: {settings.rerank_model}")
    CrossEncoder(settings.rerank_model, device="cpu")
    print("Models cached successfully.")


if __name__ == "__main__":
    main()
