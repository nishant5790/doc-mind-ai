"""
src/search_client.py — Azure AI Search wrapper.

`SearchService` provides:
* `create_or_update_index()` — idempotent index schema creation
* `index_chunks()` — upload `ChunkRecord`s with embeddings
* `search()` — hybrid search (keyword + vector) with optional doc filter
* `delete_document()` — remove all chunks for a doc_id

Index schema
------------
fields:
    id          (key, string)
    doc_id      (filterable string)
    page        (filterable int32)
    type        (filterable string)
    content     (searchable text, en.lucene)
    image_url   (string, retrievable)
    embedding   (Collection(Edm.Single), HNSW vector field)
"""

from __future__ import annotations

import logging
from typing import Optional
import json

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    VectorSearch,
    VectorSearchAlgorithmConfiguration,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

import config
from src.models import ChunkRecord, Source

log = logging.getLogger(__name__)


class SearchService:
    """Thin wrapper around an Azure AI Search index for chunk-level RAG."""

    def __init__(
        self,
        endpoint: str = config.ENDPOINT,
        index_name: str = config.INDEX_NAME,
        admin_key: Optional[str] = config.ADMIN_KEY,
    ) -> None:
        self.index_name = index_name
        cred = AzureKeyCredential(admin_key) if admin_key else config.CREDENTIAL
        self._index_client = SearchIndexClient(endpoint, cred)
        self._search_client = SearchClient(endpoint, index_name, cred)

    # ------------------------------------------------------------------
    def create_or_update_index(self) -> None:
        """Idempotently create the chunk index with HNSW vector search."""
        vector_profile = "default-vector-profile"
        algo_config = "default-hnsw"

        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),
            SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
            SimpleField(name="type", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.lucene"),
            SimpleField(name="image_url", type=SearchFieldDataType.String),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=config.EMBEDDING_DIMS,
                vector_search_profile_name=vector_profile,
            ),
        ]

        vector_search = VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name=algo_config)],
            profiles=[
                VectorSearchProfile(name=vector_profile, algorithm_configuration_name=algo_config)
            ],
        )

        index = SearchIndex(name=self.index_name, fields=fields, vector_search=vector_search)
        self._index_client.create_or_update_index(index)
        log.info("AI Search index '%s' is ready", self.index_name)

    # ------------------------------------------------------------------
    def index_chunks(self, chunks: list[ChunkRecord]) -> int:
        """Upload chunks. Returns count succeeded."""
        if not chunks:
            return 0
        docs = [c.model_dump(exclude_none=True) for c in chunks]
        results = self._search_client.upload_documents(documents=docs)
        ok = sum(1 for r in results if r.succeeded)
        log.info("Indexed %d/%d chunks", ok, len(docs))
        return ok

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        embedding: list[float],
        top_k: int = 5,
        doc_ids: Optional[list[str]] = None,
        type_filter: Optional[str] = None,
    ) -> list[Source]:
        """Hybrid search (keyword + vector). Returns lightweight `Source`s."""
        vector_query = VectorizedQuery(
            vector=embedding, k_nearest_neighbors=top_k, fields="embedding"
        )

        filter_parts: list[str] = []
        if doc_ids:
            filter_parts.append(f"search.in(doc_id, '{','.join(doc_ids)}', ',')")
        if type_filter:
            filter_parts.append(f"type eq '{type_filter}'")
        filter_expr: Optional[str] = " and ".join(filter_parts) if filter_parts else None

        results = self._search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            filter=filter_expr,
            top=top_k,
            select=["id", "doc_id", "page", "type", "content", "image_url"],
        )
        
        sources: list[Source] = []
        for r in results:
            log.debug("Raw search result: %s", r)
            sources.append(
                Source(
                    chunk_id=r["id"],
                    doc_id=r["doc_id"],
                    page=r.get("page", 0),
                    type=r.get("type", "text"),
                    snippet=(r.get("content") or "")[:400],
                    image_url=r.get("image_url"),
                )
            )
        return sources

    # ------------------------------------------------------------------
    def delete_document(self, doc_id: str) -> int:
        """Delete all chunks for a doc_id. Returns number deleted."""
        # Find chunk ids for this doc_id
        results = self._search_client.search(
            search_text="*", filter=f"doc_id eq '{doc_id}'", select=["id"], top=1000
        )
        ids = [{"id": r["id"]} for r in results]
        if not ids:
            return 0
        result = self._search_client.delete_documents(documents=ids)
        ok = sum(1 for r in result if r.succeeded)
        log.info("Deleted %d chunks for doc_id=%s", ok, doc_id)
        return ok

    # ------------------------------------------------------------------
    def wipe_index(self) -> int:
        """Drop and recreate the index — fast, total reset of all chunks.

        Returns 1 on success.
        """
        try:
            self._index_client.delete_index(self.index_name)
            log.info("Deleted AI Search index '%s'", self.index_name)
        except Exception as e:  # noqa: BLE001
            log.warning("delete_index('%s') failed (may not exist): %s", self.index_name, e)
        # Recreate empty index with same schema
        self.create_or_update_index()
        return 1
