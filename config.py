"""
config.py - Central configuration for the Azure Multimodal RAG pipeline.

Loads all environment variables from the .env file and defines derived
resource names used consistently across all modules.

Authentication strategy
-----------------------
* In production (AKS + Workload Identity) no API keys are needed.
  DefaultAzureCredential picks up the pod federated token automatically.
* For local development set the API key env vars; helpers fall back to
  AzureKeyCredential / api-key headers when the keys are present.
"""

import os
import logging

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# Logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Truststore (inject corporate CA certs into Python SSL)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
except Exception as _ts_err:
    logging.getLogger(__name__).warning("Truststore injection failed: %s", _ts_err)

# Azure AI Search
ENDPOINT: str = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
# Optional in production - not needed when Workload Identity is configured.
ADMIN_KEY: str | None = os.environ.get("AZURE_SEARCH_ADMIN_KEY")
INDEX_NAME: str = os.environ.get("AZURE_SEARCH_INDEX_NAME", "pdg-was-multimodal-rag-2")
API_VERSION: str = "2024-05-01-preview"

# Azure Blob Storage
STORAGE_ACCOUNT: str = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
CONTAINER: str = os.environ["AZURE_BLOB_CONTAINER_NAME"]
# Container that holds raw PDFs uploaded by end-users via the UI.
# Auto-created on startup if it does not exist.
USER_UPLOAD_CONTAINER: str = os.environ.get("AZURE_USER_UPLOAD_CONTAINER", "user-input")
# SAS credentials - only needed for local dev; in prod use Workload Identity.
BLOB_SAS_TOKEN: str | None = os.environ.get("AZURE_BLOB_SAS_TOKEN")
BLOB_SAS_URL: str | None = os.environ.get("BLOB_SAS_URL")

# Azure OpenAI
OPENAI_ENDPOINT: str = os.environ["AZURE_OPENAI_ENDPOINT"]
# Optional in production - Search service uses its managed identity instead.
OPENAI_KEY: str | None = os.environ.get("AZURE_OPENAI_KEY")
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_ENGINE", "text-embedding-ada-002")
EMBEDDING_DEPLOYMENT: str = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", EMBEDDING_MODEL)
EMBEDDING_DIMS: int = 1536  # ada-002 produces 1536-dimensional vectors

# Azure Document Intelligence
DOC_INTEL_ENDPOINT: str = os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
# Optional in production - managed identity is preferred.
DOC_INTEL_KEY: str | None = os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")

# Azure Cosmos DB (NoSQL API)
COSMOS_ENDPOINT: str = os.environ.get("COSMOS_ENDPOINT", "")
COSMOS_KEY: str | None = os.environ.get("COSMOS_KEY") or None
COSMOS_DATABASE: str = os.environ.get("COSMOS_DATABASE", "docmind")
# Container names (created on first use)
COSMOS_CONTAINER_SESSIONS: str = "sessions"
COSMOS_CONTAINER_DOCUMENTS: str = "documents"
COSMOS_CONTAINER_FEEDBACK: str = "feedback"
COSMOS_CONTAINER_RULES: str = "learned_rules"
COSMOS_CONTAINER_GOLDEN: str = "golden_pairs"
COSMOS_CONTAINER_CHUNK_QUALITY: str = "chunk_quality"
COSMOS_CONTAINER_TASKS: str = "ingestion_tasks"

# Chat model
GPT_ENGINE: str = os.environ.get("GPT_ENGINE", "gpt-4o")

# Azure OpenAI REST API version
OPENAI_API_VERSION: str = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

# Document Intelligence processing
DOC_INTEL_MIN_IMAGE_BYTES: int = int(os.environ.get("DOC_INTEL_MIN_IMAGE_BYTES", "5000"))
DOC_INTEL_FIGURE_RENDER_DPI: int = int(os.environ.get("DOC_INTEL_FIGURE_RENDER_DPI", "200"))
DOC_INTEL_DEDUP_IOU: float = float(os.environ.get("DOC_INTEL_DEDUP_IOU", "0.4"))
DOC_INTEL_NEIGHBOR_PARAGRAPHS_BEFORE: int = int(os.environ.get("DOC_INTEL_NEIGHBOR_BEFORE", "2"))
DOC_INTEL_NEIGHBOR_PARAGRAPHS_AFTER: int = int(os.environ.get("DOC_INTEL_NEIGHBOR_AFTER", "1"))

# Chunking
CHUNK_TOKENS: int = int(os.environ.get("CHUNK_TOKENS", "600"))
CHUNK_OVERLAP: int = int(os.environ.get("CHUNK_OVERLAP", "80"))
CHUNK_HARD_SPLIT_HEADROOM: float = float(os.environ.get("CHUNK_HARD_SPLIT_HEADROOM", "1.2"))

# RAG engine
RAG_TOP_K: int = int(os.environ.get("RAG_TOP_K", "5"))
RAG_VISUAL_INTENT_THRESHOLD: float = float(os.environ.get("RAG_VISUAL_INTENT_THRESHOLD", "0.6"))
RAG_BAD_QUALITY_THRESHOLD: float = float(os.environ.get("RAG_BAD_QUALITY_THRESHOLD", "0.3"))

# Redis chat memory
REDIS_HISTORY_LIMIT: int = int(os.environ.get("REDIS_HISTORY_LIMIT", "200"))

# Redis (chat memory) — points at local Redis in docker-compose.
# Swap to Azure Cache for Redis by setting:
#   REDIS_URL=rediss://:<access-key>@<name>.redis.cache.windows.net:6380/0
REDIS_URL: str | None = os.environ.get("REDIS_URL") or None
REDIS_PREFIX: str = os.environ.get("REDIS_PREFIX", "docmind")

# Azure AD (Entra ID) — JWT validation on FastAPI
AZURE_TENANT_ID: str | None = os.environ.get("AZURE_TENANT_ID") or None
AZURE_API_CLIENT_ID: str | None = os.environ.get("AZURE_API_CLIENT_ID") or None
AZURE_API_AUDIENCE: str | None = os.environ.get("AZURE_API_AUDIENCE") or AZURE_API_CLIENT_ID
DISABLE_AUTH: bool = os.environ.get("DOCMIND_DISABLE_AUTH", "false").lower() == "true"

# Azure subscription context (needed for managed identity blob connection)
AZURE_SUBSCRIPTION_ID: str | None = os.environ.get("AZURE_SUBSCRIPTION_ID")
AZURE_RESOURCE_GROUP: str | None = os.environ.get("AZURE_RESOURCE_GROUP")

# Managed Identity credential (used everywhere keys are absent)
# DefaultAzureCredential chain:
#   1. Env vars  (AZURE_CLIENT_ID / TENANT_ID / CLIENT_SECRET)
#   2. Workload Identity (AKS pod with federated token)
#   3. Azure CLI (local dev)
CREDENTIAL: DefaultAzureCredential = DefaultAzureCredential()

# Derived resource names
DS_NAME: str = f"{INDEX_NAME}-ds"
SKILLSET_NAME: str = f"{INDEX_NAME}-skillset"
INDEXER_NAME: str = f"{INDEX_NAME}-indexer"