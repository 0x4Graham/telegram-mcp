"""ChromaDB operations for Q&A vector storage and similarity search."""

from datetime import datetime
from typing import Optional

import chromadb
import structlog

from .config import get_chroma_path
from .embeddings import get_embedding_client

log = structlog.get_logger()

COLLECTION_NAME = "qa_pairs"
DEDUP_THRESHOLD = 0.95  # Similarity threshold for merging duplicates


class VectorStore:
    """ChromaDB vector store for Q&A pairs."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or str(get_chroma_path())
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection: Optional[chromadb.Collection] = None
        self._embedding_client = None

    def connect(self) -> None:
        """Connect to ChromaDB and get/create the collection."""
        self._client = chromadb.PersistentClient(path=self.path)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._embedding_client = get_embedding_client()
        log.info(
            "vector_store_connected",
            path=self.path,
            collection=COLLECTION_NAME,
            count=self._collection.count(),
        )

    @property
    def collection(self) -> chromadb.Collection:
        if self._collection is None:
            raise RuntimeError("VectorStore not connected. Call connect() first.")
        return self._collection

    @property
    def embedding_client(self):
        if self._embedding_client is None:
            raise RuntimeError("VectorStore not connected. Call connect() first.")
        return self._embedding_client

    def add_qa_pair(
        self,
        qa_pair_id: int,
        question: str,
        answer: str,
        chat_id: int,
        chat_name: str,
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """
        Add a Q&A pair to the vector store.
        Returns True if added, False if merged with existing (dedup).
        """
        # Check for near-duplicates first
        existing = self.query_similar(question, threshold=DEDUP_THRESHOLD, limit=1)
        if existing:
            # Merge: this is handled by the caller (suggester) to update DB too
            log.info(
                "qa_pair_duplicate_found",
                qa_pair_id=qa_pair_id,
                existing_id=existing[0]["qa_pair_id"],
                similarity=existing[0]["similarity"],
            )
            return False

        # Embed and add
        embedding = self.embedding_client.embed_single(question)

        self.collection.add(
            ids=[str(qa_pair_id)],
            embeddings=[embedding],
            documents=[question],
            metadatas=[
                {
                    "qa_pair_id": qa_pair_id,
                    "answer": answer,
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "timestamp": timestamp.isoformat() if timestamp else "",
                }
            ],
        )

        log.info("qa_pair_added", qa_pair_id=qa_pair_id, chat_name=chat_name)
        return True

    def update_qa_pair(
        self,
        qa_pair_id: int,
        question: Optional[str] = None,
        answer: Optional[str] = None,
    ) -> None:
        """Update a Q&A pair's question and/or answer."""
        # Get existing metadata
        result = self.collection.get(ids=[str(qa_pair_id)], include=["metadatas"])
        if not result["ids"]:
            log.warning("qa_pair_not_found_for_update", qa_pair_id=qa_pair_id)
            return

        metadata = result["metadatas"][0]

        if answer:
            metadata["answer"] = answer

        if question:
            # Re-embed if question changed
            embedding = self.embedding_client.embed_single(question)
            self.collection.update(
                ids=[str(qa_pair_id)],
                embeddings=[embedding],
                documents=[question],
                metadatas=[metadata],
            )
        else:
            # Just update metadata
            self.collection.update(
                ids=[str(qa_pair_id)],
                metadatas=[metadata],
            )

        log.info("qa_pair_updated", qa_pair_id=qa_pair_id)

    def delete_qa_pair(self, qa_pair_id: int) -> None:
        """Delete a Q&A pair from the vector store."""
        self.collection.delete(ids=[str(qa_pair_id)])
        log.info("qa_pair_deleted", qa_pair_id=qa_pair_id)

    def query_similar(
        self,
        question: str,
        threshold: float = 0.85,
        limit: int = 3,
    ) -> list[dict]:
        """
        Query for similar questions above the similarity threshold.
        Returns list of matches with similarity scores.
        """
        if self.collection.count() == 0:
            return []

        embedding = self.embedding_client.embed_single(question)

        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=min(limit * 2, self.collection.count()),  # Get more, filter later
            include=["documents", "metadatas", "distances"],
        )

        matches = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                # ChromaDB returns cosine distance, convert to similarity
                distance = results["distances"][0][i]
                similarity = 1 - distance

                if similarity >= threshold:
                    matches.append(
                        {
                            "qa_pair_id": int(doc_id),
                            "question": results["documents"][0][i],
                            "answer": results["metadatas"][0][i]["answer"],
                            "chat_id": results["metadatas"][0][i]["chat_id"],
                            "chat_name": results["metadatas"][0][i]["chat_name"],
                            "similarity": round(similarity, 4),
                        }
                    )

                if len(matches) >= limit:
                    break

        # Sort by similarity descending
        matches.sort(key=lambda x: x["similarity"], reverse=True)

        if matches:
            log.debug(
                "similar_questions_found",
                query=question[:50],
                count=len(matches),
                top_similarity=matches[0]["similarity"] if matches else 0,
            )

        return matches

    def get_all(self) -> list[dict]:
        """Get all Q&A pairs in the store."""
        if self.collection.count() == 0:
            return []

        results = self.collection.get(include=["documents", "metadatas"])

        pairs = []
        for i, doc_id in enumerate(results["ids"]):
            pairs.append(
                {
                    "qa_pair_id": int(doc_id),
                    "question": results["documents"][i],
                    "answer": results["metadatas"][i]["answer"],
                    "chat_id": results["metadatas"][i]["chat_id"],
                    "chat_name": results["metadatas"][i]["chat_name"],
                }
            )

        return pairs

    def count(self) -> int:
        """Get the number of Q&A pairs in the store."""
        return self.collection.count()

    def clear(self) -> None:
        """Clear all Q&A pairs from the store."""
        if self._client:
            self._client.delete_collection(COLLECTION_NAME)
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            log.info("vector_store_cleared")


# Global store instance
_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """Get the global vector store instance."""
    global _store
    if _store is None:
        _store = VectorStore()
        _store.connect()
    return _store
