"""
instagram_handler.py — Instagram Graph API + Cloudinary integration.

Publishing flow for a single image:
  1. upload_images_to_cloudinary(image_bytes_list) → [url1, ...]
  2. For one image:   _create_image_container(url, caption) → creation_id
                      _publish_container(creation_id) → ig_post_id
  3. For carousel:    _create_image_container(url, None) per slide → [child_id, ...]
                      _create_carousel_container([child_ids], caption) → creation_id
                      _publish_container(creation_id) → ig_post_id

Story flow (image + music, no stickers/text — see publish_story):
  1. _build_story_video(image_bytes, audio_path) → local mp4 path
  2. upload_video_to_cloudinary(path) → video_url
  3. _create_story_container(video_url) → creation_id
     _publish_container(creation_id) → ig_story_id
"""
import logging
import os
import subprocess
import tempfile
import time
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
        print(f"[instagram] Initialising InstagramHandler")
        print(f"[instagram]   ig_business_account_id = {ig_business_account_id}")
        print(f"[instagram]   access_token            = {access_token[:15]}...{access_token[-6:]} (len={len(access_token)})")
        print(f"[instagram]   cloudinary_cloud_name   = {cloudinary_cloud_name}")
        print(f"[instagram]   cloudinary_api_key      = {cloudinary_api_key[:6]}... (len={len(cloudinary_api_key)})")
        print(f"[instagram]   cloudinary_api_secret   = {'*' * min(len(cloudinary_api_secret), 8)} (len={len(cloudinary_api_secret)})")

        self.ig_id        = ig_business_account_id
        self.access_token = access_token
        cloudinary.config(
            cloud_name=cloudinary_cloud_name,
            api_key=cloudinary_api_key,
            api_secret=cloudinary_api_secret,
            secure=True,
        )
        print(f"[instagram] ✅ InstagramHandler ready")

    # ------------------------------------------------------------------
    # Cloudinary
    # ------------------------------------------------------------------

    def upload_images_to_cloudinary(
        self, images_bytes: list[bytes], base_public_id: str
    ) -> list[str]:
        """Upload one or more JPEG images to Cloudinary.
        Returns list of secure_url strings in the same order."""
        print(f"\n[instagram] === upload_images_to_cloudinary ===")
        print(f"[instagram] base_public_id = {base_public_id}")
        print(f"[instagram] uploading {len(images_bytes)} image(s)")

        urls = []
        for i, data in enumerate(images_bytes):
            pid = base_public_id if len(images_bytes) == 1 else f"{base_public_id}_slide{i+1}"
            size_kb = len(data) // 1024
            print(f"[instagram]   uploading slide {i+1}/{len(images_bytes)}: "
                  f"public_id={pid}, size={size_kb} KB")
            try:
                result = cloudinary.uploader.upload(
                    data,
                    public_id=pid,
                    overwrite=True,
                    resource_type='image',
                    format='jpg',
                )
                url = result['secure_url']
                urls.append(url)
                print(f"[instagram]   ✅ slide {i+1} uploaded → {url}")
                print(f"[instagram]      width={result.get('width')}, height={result.get('height')}, "
                      f"bytes={result.get('bytes')}, format={result.get('format')}")
            except Exception as e:
                print(f"[instagram]   ❌ Cloudinary upload failed for slide {i+1}: {e}")
                raise

        print(f"[instagram] all {len(urls)} image(s) uploaded")
        return urls

    def delete_from_cloudinary(self, base_public_id: str, count: int = 1):
        """Delete uploaded images from Cloudinary after successful IG publish."""
        print(f"[instagram] deleting {count} Cloudinary asset(s) with base_id={base_public_id}")
        for i in range(count):
            pid = base_public_id if count == 1 else f"{base_public_id}_slide{i+1}"
            try:
                result = cloudinary.uploader.destroy(pid, resource_type='image')
                print(f"[instagram]   deleted {pid} → result={result}")
            except Exception as e:
                print(f"[instagram]   ⚠️  delete failed for {pid}: {e}")

    def upload_video_to_cloudinary(self, video_path: str, public_id: str) -> str:
        """Upload a local mp4 to Cloudinary as a video resource. Returns secure_url."""
        print(f"[instagram] uploading story video to Cloudinary: public_id={public_id}")
        result = cloudinary.uploader.upload(
            video_path,
            public_id=public_id,
            overwrite=True,
            resource_type='video',
        )
        url = result['secure_url']
        print(f"[instagram]   ✅ story video uploaded → {url} "
              f"(duration={result.get('duration')}, bytes={result.get('bytes')})")
        return url

    def delete_video_from_cloudinary(self, public_id: str):
        """Delete an uploaded story video from Cloudinary after successful IG publish."""
        try:
            result = cloudinary.uploader.destroy(public_id, resource_type='video')
            print(f"[instagram]   deleted video {public_id} → result={result}")
        except Exception as e:
            print(f"[instagram]   ⚠️  video delete failed for {public_id}: {e}")

    # ------------------------------------------------------------------
    # Instagram Graph API
    # ------------------------------------------------------------------

    def _post(self, path: str, data: dict) -> dict:
        url      = f"{self.API_BASE}/{path}"
        safe_data = {k: (v[:30] + '...' if isinstance(v, str) and len(v) > 30 else v)
                     for k, v in data.items() if k != 'access_token'}
        print(f"[instagram] POST {url}")
        print(f"[instagram]   payload (no token): {safe_data}")

        full_data = {**data, 'access_token': self.access_token}
        try:
            resp = requests.post(url, data=full_data, timeout=30)
        except Exception as e:
            print(f"[instagram] ❌ request exception: {e}")
            raise

        print(f"[instagram]   HTTP {resp.status_code}")
        body = resp.json()
        print(f"[instagram]   response body: {body}")

        if resp.status_code != 200 or 'error' in body:
            raise Exception(f"IG API error on POST {path}: {body}")
        return body

    def _get(self, path: str, params: dict = None) -> dict:
        url    = f"{self.API_BASE}/{path}"
        safe_p = {k: v for k, v in (params or {}).items() if k != 'access_token'}
        print(f"[instagram] GET {url} params={safe_p}")

        full_params = {**(params or {}), 'access_token': self.access_token}
        try:
            resp = requests.get(url, params=full_params, timeout=10)
        except Exception as e:
            print(f"[instagram] ❌ request exception: {e}")
            raise

        print(f"[instagram]   HTTP {resp.status_code}")
        body = resp.json()
        print(f"[instagram]   response body: {body}")

        if resp.status_code != 200 or 'error' in body:
            raise Exception(f"IG API error on GET {path}: {body}")
        return body

    def _create_image_container(self, image_url: str,
                                caption: Optional[str],
                                is_carousel_item: bool = False) -> str:
        """Create a single-image media container. Returns creation_id."""
        print(f"\n[instagram] _create_image_container(is_carousel_item={is_carousel_item})")
        print(f"[instagram]   image_url = {image_url}")
        if caption:
            print(f"[instagram]   caption ({len(caption)} chars) = {caption[:80]}...")

        data: dict = {'image_url': image_url}
        if is_carousel_item:
            data['is_carousel_item'] = 'true'
        elif caption is not None:
            data['caption'] = caption

        body = self._post(f"{self.ig_id}/media", data)
        creation_id = body['id']
        print(f"[instagram] ✅ container created: {creation_id}")
        return creation_id

    STORY_DURATION_SECONDS = 15

    def _build_story_video(self, image_bytes: bytes, audio_path: str,
                            duration: int = STORY_DURATION_SECONDS) -> str:
        """Render a 1080x1920 story video: a blurred, cropped copy of the image
        fills the background, the original square image sits centered on top,
        and the given audio track is muxed in — no stickers, no text, no overlays.
        Returns the local mp4 path (caller deletes it after upload)."""
        tmp_dir    = tempfile.mkdtemp(prefix='ig_story_')
        image_path = os.path.join(tmp_dir, 'cover.jpg')
        out_path   = os.path.join(tmp_dir, 'story.mp4')
        with open(image_path, 'wb') as f:
            f.write(image_bytes)

        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[0:v]scale=1080:1080[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', image_path,
            '-i', audio_path,
            '-filter_complex', filter_complex,
            '-map', '[v]', '-map', '1:a',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
            '-shortest', '-t', str(duration),
            out_path,
        ]
        print(f"[instagram]   building story video (duration={duration}s)")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(out_path):
            raise Exception(f"ffmpeg failed building story video: {result.stderr[-2000:]}")
        print(f"[instagram]   ✅ story video rendered: {out_path} "
              f"({os.path.getsize(out_path) // 1024} KB)")
        return out_path

    def _create_story_container(self, video_url: str) -> str:
        """Create a Story media container from a video. Returns creation_id."""
        print(f"\n[instagram] _create_story_container")
        print(f"[instagram]   video_url = {video_url}")
        body = self._post(f"{self.ig_id}/media", {
            'media_type': 'STORIES',
            'video_url':  video_url,
        })
        creation_id = body['id']
        print(f"[instagram] ✅ story container created: {creation_id}")
        return creation_id

    def _create_carousel_container(self, child_ids: list[str], caption: str) -> str:
        """Create the carousel container that wraps child image containers."""
        print(f"\n[instagram] _create_carousel_container({len(child_ids)} children)")
        print(f"[instagram]   child_ids = {child_ids}")
        print(f"[instagram]   caption ({len(caption)} chars) = {caption[:80]}...")

        body = self._post(f"{self.ig_id}/media", {
            'media_type': 'CAROUSEL',
            'children':   ','.join(child_ids),
            'caption':    caption,
        })
        creation_id = body['id']
        print(f"[instagram] ✅ carousel container created: {creation_id}")
        return creation_id

    # Error subcode Meta returns while a media container is still processing
    # ("המדיה לא מוכנה לפרסום, יש להמתין רגע" / "Media ID is not available").
    _MEDIA_NOT_READY_SUBCODE = 2207027

    def _wait_for_container_ready(self, creation_id: str,
                                   max_wait_seconds: int = 60,
                                   poll_interval: int = 3) -> None:
        """Poll a media container's status_code until FINISHED. Raises on ERROR
        or timeout. Best-effort: swallows lookup errors and just proceeds,
        since _publish_container has its own retry for the not-ready case."""
        waited = 0
        while waited <= max_wait_seconds:
            try:
                body = self._get(creation_id, {'fields': 'status_code'})
            except Exception as e:
                print(f"[instagram]   status check failed for {creation_id}, proceeding anyway: {e}")
                return
            status = body.get('status_code')
            print(f"[instagram]   container {creation_id} status_code={status} (waited {waited}s)")
            if status == 'FINISHED':
                return
            if status == 'ERROR':
                raise Exception(f"IG media container {creation_id} failed processing (status_code=ERROR)")
            time.sleep(poll_interval)
            waited += poll_interval
        print(f"[instagram]   ⚠️ container {creation_id} still not FINISHED after {max_wait_seconds}s, trying publish anyway")

    def _publish_container(self, creation_id: str,
                            max_retries: int = 4,
                            retry_delay: int = 5,
                            max_wait_seconds: int = 60) -> str:
        """Publish a media container. Returns the live IG post ID.
        Retries with backoff if Instagram reports the media isn't ready yet
        (error_subcode 2207027) — this is transient and normally resolves
        within a few seconds for images, longer for video (stories)."""
        print(f"\n[instagram] _publish_container(creation_id={creation_id})")

        self._wait_for_container_ready(creation_id, max_wait_seconds=max_wait_seconds)

        attempt = 0
        while True:
            attempt += 1
            try:
                body = self._post(f"{self.ig_id}/media_publish", {
                    'creation_id': creation_id,
                })
                ig_post_id = body['id']
                print(f"[instagram] ✅ published! ig_post_id = {ig_post_id}")
                return ig_post_id
            except Exception as e:
                not_ready = str(self._MEDIA_NOT_READY_SUBCODE) in str(e)
                if not_ready and attempt <= max_retries:
                    wait = retry_delay * attempt
                    print(f"[instagram]   media not ready (attempt {attempt}/{max_retries}), "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                raise

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
        print(f"\n[instagram] ========== publish_post ==========")
        print(f"[instagram] ig_id          = {self.ig_id}")
        print(f"[instagram] base_public_id = {base_public_id}")
        print(f"[instagram] slides count   = {len(images_bytes)}")
        print(f"[instagram] caption length = {len(caption)} chars")
        print(f"[instagram] caption preview: {caption[:120]}...")

        n = len(images_bytes)

        # Step 1: Cloudinary upload
        print(f"\n[instagram] STEP 1: Upload to Cloudinary")
        urls = self.upload_images_to_cloudinary(images_bytes, base_public_id)

        # Step 2: IG API
        if n == 1:
            print(f"\n[instagram] STEP 2a: Single-image publish")
            creation_id = self._create_image_container(urls[0], caption)
        else:
            print(f"\n[instagram] STEP 2a: Carousel — creating {n} child containers")
            child_ids = []
            for idx, url in enumerate(urls):
                print(f"[instagram]   child {idx+1}/{n}: {url}")
                cid = self._create_image_container(url, caption=None, is_carousel_item=True)
                child_ids.append(cid)
                print(f"[instagram]   child {idx+1} id = {cid}")
            print(f"\n[instagram] STEP 2b: Creating carousel container")
            creation_id = self._create_carousel_container(child_ids, caption)

        print(f"\n[instagram] STEP 3: Publishing container {creation_id}")
        ig_post_id = self._publish_container(creation_id)

        print(f"\n[instagram] STEP 4: Cleanup Cloudinary")
        self.delete_from_cloudinary(base_public_id, count=n)

        result = {'ig_post_id': ig_post_id, 'slides': n}
        print(f"\n[instagram] ✅ publish_post complete: {result}")
        print(f"[instagram] ==========================================")
        return result

    def publish_story(self, image_bytes: bytes, audio_path: str, base_public_id: str) -> dict:
        """
        Publish a Story made from one cover image + a music track, nothing else
        (no stickers, no text, no links — just image and sound):
          1. Render a vertical mp4 locally with ffmpeg.
          2. Upload it to Cloudinary (video).
          3. Create a STORIES media container and publish it.
          4. Clean up the local file and the Cloudinary asset.
        Returns {'ig_story_id': str}.
        """
        print(f"\n[instagram] ========== publish_story ==========")
        print(f"[instagram] base_public_id = {base_public_id}")
        print(f"[instagram] audio_path     = {audio_path}")

        video_path = self._build_story_video(image_bytes, audio_path)
        try:
            video_url = self.upload_video_to_cloudinary(video_path, base_public_id)
        finally:
            try:
                os.remove(video_path)
                os.rmdir(os.path.dirname(video_path))
            except Exception:
                pass

        creation_id = self._create_story_container(video_url)
        # Video containers take longer to process than images.
        ig_story_id = self._publish_container(creation_id, max_wait_seconds=180)

        self.delete_video_from_cloudinary(base_public_id)

        result = {'ig_story_id': ig_story_id}
        print(f"\n[instagram] ✅ publish_story complete: {result}")
        print(f"[instagram] ==========================================")
        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def was_recently_posted(self, label, limit: int = 25) -> bool:
        """Return True if a recent Instagram post already has this caption label
        (its caption's first line equals `label`, e.g. '#15482' or 'אינסטוש#5').
        Used to avoid duplicates: Instagram sometimes creates a post even when the
        publish call returns an error, so we verify before (re)publishing. On any
        error returns False."""
        try:
            body = self._get(f"{self.ig_id}/media", {'fields': 'caption', 'limit': limit})
            target = str(label).strip()
            for m in body.get('data', []):
                cap = (m.get('caption') or '').strip()
                if cap.split('\n', 1)[0].strip() == target:
                    return True
        except Exception as e:
            print(f"[instagram] was_recently_posted check failed: {e}")
        return False

    def test_connection(self) -> bool:
        """Return True if the IG Business Account ID is reachable."""
        print(f"[instagram] test_connection() for ig_id={self.ig_id}")
        try:
            data = self._get(self.ig_id, {'fields': 'id,name,username'})
            print(f"[instagram] ✅ connection OK: {data}")
            return True
        except Exception as e:
            print(f"[instagram] ❌ connection failed: {e}")
            return False
