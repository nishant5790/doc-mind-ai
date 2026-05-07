"""03 — GPT-4o vision smoke test (mirrors notebooks/03_openai_vision.ipynb)."""
import _bootstrap

from src.blob_client import BlobService
from src.openai_client import OpenAIService


def main() -> None:
    img_path = _bootstrap.find_asset("sample.png", "sample.jpg", "sample.jpeg")
    print("Using image:", img_path)

    blob = BlobService()
    blob.ensure_container()
    name = f"docmind-test/vision-{img_path.name}"
    blob.upload(name, img_path.read_bytes(), content_type=f"image/{img_path.suffix.lstrip('.')}")
    url = blob.get_url(name)
    print("Image URL:", url[:80], "...")

    ai = OpenAIService()
    desc = ai.describe_image(url)
    print("--- vision description ---")
    print(desc)

    v = ai.embed("Hello DocMind")[0]
    print("embedding dim:", len(v))


if __name__ == "__main__":
    main()
