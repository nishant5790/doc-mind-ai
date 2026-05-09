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
    id              (key, string)
    doc_id          (filterable string)         — UUID assigned at upload
    doc_filename    (filterable string)         — original PDF filename
    doc_hash        (filterable string)         — sha256 of source bytes (stable doc identity)
    page            (filterable int32)
    type            (filterable string)         — text | table | image
    source          (filterable string)         — figure | raster (image chunks)
    section_id      (filterable string)         — DI section id
    section_path    (searchable+filterable str) — "1. Intro > Background"
    section_level   (filterable int32)
    parent_id       (filterable string)         — anchor text-chunk for this section
    element_id      (filterable string)         — DI ref e.g. /tables/3, /figures/1
    reading_order   (filterable+sortable int32)
    bbox            (Collection(Edm.Double))    — [x0,y0,x1,y1] PDF points on `page`
    content         (searchable text, en.lucene)
    caption         (searchable text, en.lucene)
    image_url       (string, retrievable)
    embedding       (Collection(Edm.Single), HNSW vector field)
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
    ExhaustiveKnnAlgorithmConfiguration  ,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
)
from azure.search.documents.models import VectorizedQuery, QueryType

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
            SimpleField(name="doc_filename", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="doc_hash", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
            SimpleField(name="type", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="source", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="section_id", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="section_path", type=SearchFieldDataType.String, filterable=True, facetable=True, analyzer_name="en.lucene"),
            SimpleField(name="section_level", type=SearchFieldDataType.Int32, filterable=True),
            SimpleField(name="parent_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="element_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="reading_order", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
            SimpleField(
                name="bbox",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Double),
            ),
            SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.lucene"),
            SearchableField(name="caption", type=SearchFieldDataType.String, analyzer_name="en.lucene"),
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
            algorithms=[HnswAlgorithmConfiguration(name=algo_config),
                        ExhaustiveKnnAlgorithmConfiguration(name="my-eknn-vector-config", kind="exhaustiveKnn") ],
            profiles=[
                VectorSearchProfile(name=vector_profile, algorithm_configuration_name=algo_config)
            ],
        )

        # Semantic configuration — used by the L2 reranker to promote the most
        # relevant chunks. Prioritising `section_path` as the title field and
        # `doc_filename` as a keyword keeps each chunk anchored to its own
        # document, which reduces cross-PDF mixing in top results.
        semantic_config = SemanticConfiguration(
            name=config.SEMANTIC_CONFIG_NAME,
            prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="section_path"),
                keywords_fields=[
                    SemanticField(field_name="doc_filename"),
                ],
                content_fields=[SemanticField(field_name="content")],
            ),
        )
        semantic_search = SemanticSearch(configurations=[semantic_config])

        index = SearchIndex(
            name=self.index_name,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search,
        )
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
        source_filter: Optional[str] = None,
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
        if source_filter:
            filter_parts.append(f"source eq '{source_filter}'")
        filter_expr: Optional[str] = " and ".join(filter_parts) if filter_parts else None

        results = self._search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            filter=filter_expr,
            top=top_k,
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name=config.SEMANTIC_CONFIG_NAME,
            query_caption="extractive",
            select=[
                "id", "doc_id", "doc_filename", "page", "type", "content",
                "image_url", "caption", "source",
                "section_path", "parent_id",
            ],
        )  # type: ignore

        
        sources: list[Source] = []
        for r in results:
            log.debug("Raw search result: %s", r)
            # Prefer the semantic reranker score when available so downstream
            # ranking reflects L2 relevance rather than raw BM25/vector score.
            reranker_score = r.get("@search.reranker_score")
            score = reranker_score if reranker_score is not None else r.get("@search.score")

            # If a semantic caption came back, surface the highlighted/extracted
            # snippet — it is usually a tighter, more on-topic excerpt than the
            # raw chunk content.
            snippet = (r.get("content") or "")[:400]
            captions = r.get("@search.captions") or []
            if captions:
                cap = captions[0]
                cap_text = getattr(cap, "highlights", None) or getattr(cap, "text", None)
                if cap_text:
                    snippet = cap_text[:400]

            sources.append(
                Source(
                    chunk_id=r["id"],
                    doc_id=r["doc_id"],
                    doc_filename=r.get("doc_filename"),
                    page=r.get("page", 0),
                    type=r.get("type", "text"),
                    snippet=snippet,
                    image_url=r.get("image_url"),
                    caption=r.get("caption"),
                    source=r.get("source"),
                    section_path=r.get("section_path"),
                    parent_id=r.get("parent_id"),
                    score=score,
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
