"""
instagram_handler.py — Instagram Graph API + Cloudinary integration.

Publishing flow for a single image:
  1. upload_images_to_cloudinary(image_bytes_list) → [url1, ...]
  2. For one image:   _create_image_container(url, caption) → creation_id
                      _publish_container(creation_id) → ig_post_id
  3. For carousel:    _create_image_container(url, None) per slide → [child_id, ...]
                      _create_carousel_container([child_ids], caption) → creation_id
                      _publish_container(creation_id) → ig_post_id
"""
import logging
from typing import Optional

import cloudinary
import cloudinary.uploader
import requests

log = logging.getLogger(__name__)


class InstagramHandler:
    API_BASE = "https://graph.facebook.com/v21.0"

    def __init__(
        self,
        ig_business_account_id: str,
        access_token: str,
        cloudinary_cloud_name: str,
        cloudinary_api_key: str,
        cloudinary_api_secret: str,
    ):
        self.ig_id        = ig_business_account_id
        self.access_token = access_token
        cloudinary.config(
            cloud_name=cloudinary_cloud_name,
            api_key=cloudinary_api_key,
            api_secret=cloudinary_api_secret,
            secure=True,
        )

    # ------------------------------------------------------------------
    # Cloudinary
    # ------------------------------------------------------------------

    def upload_images_to_cloudinary(
        self, images_bytes: list[bytes], base_public_id: str
    ) -> list[str]:
        """Upload one or more JPEG images to Cloudinary.
        Returns list of secure_url strings in the same order.
        Raises on any failure."""
        urls = []
        for i, data in enumerate(images_bytes):
            public_id = base_public_id if len(images_bytes) == 1 else f"{base_public_id}_slide{i+1}"
            result = cloudinary.uploader.upload(
                data,
                public_id=public_id,
                overwrite=True,
                resource_type='image',
                format='jpg',
            )
            urls.append(result['secure_url'])
            log.info("[instagram] uploaded slide %d → %s", i + 1, result['secure_url'])
        return urls

    def delete_from_cloudinary(self, base_public_id: str, count: int = 1):
        """Delete uploaded images from Cloudinary after successful IG publish."""
        for i in range(count):
            pid = base_public_id if count == 1 else f"{base_public_id}_slide{i+1}"
            try:
                cloudinary.uploader.destroy(pid, resource_type='image')
                log.debug("[instagram] deleted cloudinary asset: %s", pid)
            except Exception as e:
                log.warning("[instagram] cloudinary delete failed for %s: %s", pid, e)

    # ------------------------------------------------------------------
    # Instagram Graph API
    # ------------------------------------------------------------------

    def _post(self, path: str, data: dict) -> dict:
        url  = f"{self.API_BASE}/{path}"
        data = {**data, 'access_token': self.access_token}
        resp = requests.post(url, data=data, timeout=30)
        body = resp.json()
        if resp.status_code != 200 or 'error' in body:
            raise Exception(f"IG API error on {path}: {body}")
        return body

    def _get(self, path: str, params: dict = None) -> dict:
        url    = f"{self.API_BASE}/{path}"
        params = {**(params or {}), 'access_token': self.access_token}
        resp   = requests.get(url, params=params, timeout=10)
        body   = resp.json()
        if resp.status_code != 200 or 'error' in body:
            raise Exception(f"IG API error on GET {path}: {body}")
        return body

    def _create_image_container(self, image_url: str,
                                caption: Optional[str],
                                is_carousel_item: bool = False) -> str:
        """Create a single-image media container. Returns creation_id."""
        data: dict = {'image_url': image_url}
        if is_carousel_item:
            data['is_carousel_item'] = 'true'
        elif caption is not None:
            data['caption'] = caption
        body = self._post(f"{self.ig_id}/media", data)
        return body['id']

    def _create_carousel_container(self, child_ids: list[str], caption: str) -> str:
        """Create the carousel container that wraps child image containers."""
        body = self._post(f"{self.ig_id}/media", {
            'media_type': 'CAROUSEL',
            'children':   ','.join(child_ids),
            'caption':    caption,
        })
        return body['id']

    def _publish_container(self, creation_id: str) -> str:
        """Publish a media container. Returns the live IG post ID."""
        body = self._post(f"{self.ig_id}/media_publish", {
            'creation_id': creation_id,
        })
        return body['id']

    # ------------------------------------------------------------------
    # High-level publish
    # ------------------------------------------------------------------

    def publish_post(
        self,
        images_bytes: list[bytes],
        caption: str,
        base_public_id: str,
    ) -> dict:
        """
        Full publish flow:
          1. Upload images to Cloudinary.
          2. Create IG container(s) and publish.
          3. Delete from Cloudinary after success.
        Returns {'ig_post_id': str, 'slides': int}.
        """
        urls   = self.upload_images_to_cloudinary(images_bytes, base_public_id)
        n      = len(urls)

        if n == 1:
            creation_id = self._create_image_container(urls[0], caption)
        else:
            child_ids = [
                self._create_image_container(url, caption=None, is_carousel_item=True)
                for url in urls
            ]
            creation_id = self._create_carousel_container(child_ids, caption)

        ig_post_id = self._publish_container(creation_id)
        log.info("[instagram] published ig_post_id=%s (%d slide(s))", ig_post_id, n)

        self.delete_from_cloudinary(base_public_id, count=n)
        return {'ig_post_id': ig_post_id, 'slides': n}

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Return True if the IG Business Account ID is reachable."""
        try:
            self._get(self.ig_id, {'fields': 'id,name'})
            return True
        except Exception:
            return False
