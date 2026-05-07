"""02 — Document Intelligence smoke test (mirrors notebooks/02_doc_intelligence.ipynb)."""
import _bootstrap

from src.blob_client import BlobService
from src.doc_intelligence import DocIntelService
from src.openai_client import OpenAIService


def main() -> None:
    pdf_path = _bootstrap.find_asset(
        "Multi_Agent_Research_System_Architecture.pdf",
        "sample.pdf",
    )
    print("Using PDF:", pdf_path)

    blob = BlobService()
    blob.ensure_container()
    name = "docmind-test/sample.pdf"
    pdf_bytes = pdf_path.read_bytes()
    blob.upload(name, pdf_bytes, content_type="application/pdf")
    url = blob.get_url(name)
    print("PDF URL:", url[:80], "...")

    doc_intel = DocIntelService()
    result = doc_intel.extract_pdf(url)
    print("pages:       ", result["pages"])
    print("text chunks: ", len(result["text_chunks"]))
    print("tables:      ", len(result["tables"]))
    if result["text_chunks"]:
        print("--- first 600 chars of page 1 ---")
        print(result["text_chunks"][0]["content"][:600])

    if result["tables"]:
        print("--- first table (truncated) ---")
        print(result["tables"][0]["content"][:500])
    else:
        print("No tables detected.")

    openai = OpenAIService()
    images = doc_intel.extract_images(
        pdf_bytes,
        doc_id="docmind-test",
        blob=blob,
        openai=openai,
    )
    print(f"extracted {len(images)} images")
    for img in images[:5]:
        print(
            f"- page {img['page']} {img['ext']} "
            f"{img['size_bytes']} bytes -> {img['blob_name']}"
        )


if __name__ == "__main__":
    main()
