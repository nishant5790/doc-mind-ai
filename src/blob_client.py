"""
src/blob_client.py — Azure Blob Storage wrapper.

The `BlobService` class provides a small, focused API for uploading,
downloading, listing, and deleting blobs in the configured container.

Authentication
--------------
* Local dev — uses the SAS token from `.env` (`AZURE_BLOB_SAS_TOKEN`).
* Production — uses `DefaultAzureCredential` (Workload Identity).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from azure.storage.blob import BlobServiceClient, ContainerClient, generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger(__name__)


class BlobService:
    """Thin wrapper around a single Blob container."""

    def __init__(
        self,
        account: str = config.STORAGE_ACCOUNT,
        container: str = config.CONTAINER,
        sas_token: Optional[str] = config.BLOB_SAS_TOKEN,
    ) -> None:
        self.account = account
        self.container = container
        self._sas_token = sas_token

        account_url = f"https://{account}.blob.core.windows.net"
        if sas_token:
            self._service: BlobServiceClient = BlobServiceClient(
                account_url=account_url, credential=sas_token
            )
        else:
            self._service = BlobServiceClient(account_url=account_url, credential=config.CREDENTIAL)
        self._container: ContainerClient = self._service.get_container_client(container)

    # ------------------------------------------------------------------
    def ensure_container(self) -> None:
        """Create the container if it does not exist."""
        try:
            self._container.create_container()
            log.info("Created container %s", self.container)
        except Exception:  # already exists
            pass

    # ------------------------------------------------------------------
    def ensure_container_named(self, container: str) -> None:
        """Create an arbitrary container on the same account if it does not exist."""
        try:
            self._service.get_container_client(container).create_container()
            log.info("Created container %s", container)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def download_from(self, container: str, name: str) -> bytes:
        """Download bytes from any container on this storage account."""
        return self._service.get_container_client(container).get_blob_client(name).download_blob().readall()

    # ------------------------------------------------------------------
    def url_for(self, container: str, name: str) -> str:
        """Build a (SAS-appended) URL for a blob in any container on this account."""
        base = f"https://{self.account}.blob.core.windows.net/{container}/{name}"
        if self._sas_token:
            return f"{base}?{self._sas_token.lstrip('?')}"
        return base

    # ------------------------------------------------------------------
    def upload(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Upload bytes to a blob and return the blob URL."""
        from azure.storage.blob import ContentSettings

        blob = self._container.get_blob_client(name)
        blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        log.info("Uploaded blob %s (%d bytes)", name, len(data))
        return blob.url

    # ------------------------------------------------------------------
    def download(self, name: str) -> bytes:
        blob = self._container.get_blob_client(name)
        return blob.download_blob().readall()

    # ------------------------------------------------------------------
    def list_blobs(self, prefix: str = "") -> Iterable[str]:
        for b in self._container.list_blobs(name_starts_with=prefix):
            yield b.name

    # ------------------------------------------------------------------
    def delete(self, name: str) -> None:
        try:
            self._container.delete_blob(name)
            log.info("Deleted blob %s", name)
        except Exception as e:
            log.warning("Delete blob %s failed: %s", name, e)

    # ------------------------------------------------------------------
    def delete_all(self, prefix: str = "") -> int:
        """Delete every blob in the container (optionally under a prefix).

        Returns the count of blobs deleted.
        """
        count = 0
        for name in list(self.list_blobs(prefix=prefix)):
            try:
                self._container.delete_blob(name)
                count += 1
            except Exception as e:  # noqa: BLE001
                log.warning("delete_all: could not delete %s: %s", name, e)
        log.info("delete_all: removed %d blobs (prefix=%r) from %s", count, prefix, self.container)
        return count

    # ------------------------------------------------------------------
    def get_url(self, name: str) -> str:
        """Return a URL for the blob.

        If a SAS token is configured we append it (works with private
        containers); otherwise return the bare blob URL (managed identity).
        """
        base = f"https://{self.account}.blob.core.windows.net/{self.container}/{name}"
        if self._sas_token:
            return f"{base}?{self._sas_token.lstrip('?')}"
        return base

    # ------------------------------------------------------------------
    def get_user_delegation_sas(self, name: str, hours: int = 1) -> str:
        """Generate a short-lived read SAS using the account SAS (if present).

        Useful when calling Document Intelligence / GPT-4o vision with a
        URL that needs to be readable.
        """
        # If we already have a container SAS, just reuse it.
        if self._sas_token:
            return self.get_url(name)

        # Otherwise fall back to user-delegation SAS via managed identity.
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        udk = self._service.get_user_delegation_key(start, expiry)
        sas = generate_blob_sas(
            account_name=self.account,
            container_name=self.container,
            blob_name=name,
            user_delegation_key=udk,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            start=start,
        )
        return f"https://{self.account}.blob.core.windows.net/{self.container}/{name}?{sas}"
