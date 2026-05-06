"""
src/openai_client.py — Azure OpenAI wrapper.

`OpenAIService` exposes three small methods used across the system:

* `chat()` — streaming or non-streaming chat completion (gpt-4o)
* `embed()` — generate embeddings (text-embedding-ada-002)
* `describe_image()` — multimodal vision call to describe an image
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Iterable, Optional

from openai import AzureOpenAI, AsyncAzureOpenAI

import config

log = logging.getLogger(__name__)

API_VERSION = "2024-08-01-preview"


class OpenAIService:
    """Wrapper around Azure OpenAI for chat, embeddings, and vision."""

    def __init__(
        self,
        endpoint: str = config.OPENAI_ENDPOINT,
        api_key: Optional[str] = config.OPENAI_KEY,
        chat_deployment: str = config.GPT_ENGINE,
        embed_deployment: str = config.EMBEDDING_DEPLOYMENT,
    ) -> None:
        self.chat_deployment = chat_deployment
        self.embed_deployment = embed_deployment

        kwargs = dict(azure_endpoint=endpoint, api_version=API_VERSION)
        if api_key:
            self._client = AzureOpenAI(api_key=api_key, **kwargs)
            self._async_client = AsyncAzureOpenAI(api_key=api_key, **kwargs)
        else:
            from azure.identity import get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                config.CREDENTIAL, "https://cognitiveservices.azure.com/.default"
            )
            self._client = AzureOpenAI(azure_ad_token_provider=token_provider, **kwargs)
            self._async_client = AsyncAzureOpenAI(azure_ad_token_provider=token_provider, **kwargs)

    # ------------------------------------------------------------------
    def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 1024) -> str:
        resp = self._client.chat.completions.create(
            model=self.chat_deployment,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    # ------------------------------------------------------------------
    async def stream_chat(
        self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Async streaming chat completion — yields text tokens as they arrive."""
        stream = await self._async_client.chat.completions.create(
            model=self.chat_deployment,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # ------------------------------------------------------------------
    def embed(self, text: str | Iterable[str]) -> list[list[float]]:
        """Generate embeddings for one or more strings."""
        inputs = [text] if isinstance(text, str) else list(text)
        resp = self._client.embeddings.create(model=self.embed_deployment, input=inputs)
        return [d.embedding for d in resp.data]

    # ------------------------------------------------------------------
    def describe_image(self, image_url: str, prompt: Optional[str] = None) -> str:
        """Use GPT-4o vision to describe an image at `image_url`."""
        prompt = prompt or (
            "Describe this image in detail for a knowledge base. "
            "If it is a chart/graph, extract the data points. "
            "If it is a diagram, describe the components and relationships. "
            "If it contains text, transcribe it exactly. "
            "Be concise but complete."
        )
        resp = self._client.chat.completions.create(
            model=self.chat_deployment,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=600,
        )
        return resp.choices[0].message.content or ""
