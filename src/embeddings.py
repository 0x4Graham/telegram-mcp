"""Local embedding client using sentence-transformers."""

from typing import Optional

import structlog
from sentence_transformers import SentenceTransformer

log = structlog.get_logger()

# Default model - good balance of speed and quality
DEFAULT_MODEL = "all-MiniLM-L6-v2"


class EmbeddingClient:
    """Local embedding client using sentence-transformers."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model_name = model
        log.info("loading_embedding_model", model=model)
        self._model = SentenceTransformer(model)
        log.info("embedding_model_loaded", model=model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts."""
        if not texts:
            return []

        embeddings = self._model.encode(texts, convert_to_numpy=True)
        log.debug("texts_embedded", count=len(texts), model=self.model_name)
        return embeddings.tolist()

    def embed_single(self, text: str) -> list[float]:
        """Embed a single text."""
        embedding = self._model.encode([text], convert_to_numpy=True)
        return embedding[0].tolist()


# Global client instance
_client: Optional[EmbeddingClient] = None


def get_embedding_client(model: str = DEFAULT_MODEL) -> EmbeddingClient:
    """Get the global embedding client instance."""
    global _client
    if _client is None:
        _client = EmbeddingClient(model=model)
    return _client
