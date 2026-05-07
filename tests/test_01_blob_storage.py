"""01 — Blob Storage smoke test (mirrors notebooks/01_blob_storage.ipynb)."""
import _bootstrap  # noqa: F401

from src.blob_client import BlobService


def main() -> None:
    blob = BlobService()
    blob.ensure_container()
    print("Container:", blob.container)

    name = "docmind-test/hello.txt"

    url = blob.upload(name, b"hello docmind", content_type="text/plain")
    print("Uploaded:", url)

    print("List (first 10):")
    for n in list(blob.list_blobs(prefix="docmind-test/"))[:10]:
        print(" -", n)

    data = blob.download(name)
    print("Downloaded:", data)

    blob.delete(name)
    print("Deleted OK")


if __name__ == "__main__":
    main()
