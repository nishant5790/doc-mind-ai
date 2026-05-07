"""06 — End-to-end ingestion pipeline (mirrors notebooks/06_ingestion_pipeline.ipynb)."""
import _bootstrap

from src.blob_client import BlobService
from src.search_client import SearchService
from src.doc_intelligence import DocIntelService
from src.openai_client import OpenAIService
from src.cosmos_client import create_state_service
from src.ingestion import IngestionPipeline
from src.models import DocumentMeta


def main() -> None:
    pdf_path = _bootstrap.find_asset(
        "Multi_Agent_Research_System_Architecture.pdf",
        "sample.pdf",
    )
    print("Using PDF:", pdf_path)

    blob = BlobService()
    blob.ensure_container()
    search = SearchService()
    search.create_or_update_index()
    cosmos = create_state_service()
    cosmos.ensure_containers()
    print("State backend:", type(cosmos).__name__)

    pipeline = IngestionPipeline(
        blob, DocIntelService(), OpenAIService(), search, cosmos
    )

    doc = DocumentMeta(
        user_id="nb-user",
        filename=pdf_path.name,
        blob_name=f"nb-user/{pdf_path.name}",
    )
    blob.upload(doc.blob_name, pdf_path.read_bytes(), content_type="application/pdf")
    cosmos.save_document(doc)
    print("Doc id:", doc.id)

    doc = pipeline.process_pdf(doc)
    print("Status :", doc.status)
    print("Pages  :", doc.total_pages)
    print("Chunks :", doc.total_chunks)
    print("Images :", doc.total_images)
    print("Tables :", doc.total_tables)


if __name__ == "__main__":
    main()
