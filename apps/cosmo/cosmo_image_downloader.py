"""Download the latest photo from the Cosmo album and convert to WebP."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Final

from appdaemon.plugins.hass.hassapi import Hass  # type: ignore[import-not-found]
from PIL import Image
from pydantic import field_validator
from wg_utilities.clients import GooglePhotosClient
from wg_utilities.clients.google_photos import Album, MediaItem, MediaType

PHOTOS_DIRECTORY: Final[Path] = Path("/homeassistant/www/images/cosmo")


class WebPImage(MediaItem):
    """MediaItem subclass specifically for WebP images."""

    _local_path: Path

    @field_validator("filename")
    @classmethod
    def validate_webp_filename(cls, value: str) -> str:
        """Validate that the filename is for a WebP image."""
        *parts, _ = value.split(".")
        return ".".join([*parts, "webp"])

    def download(
        self,
        target_directory: Path | str = "",
        *,
        file_name_override: str | None = None,
        width_override: int | None = None,
        height_override: int | None = None,
        force_download: bool = False,
    ) -> Path:
        """Download the media item, convert to WebP and save to local storage."""
        del target_directory, file_name_override, force_download

        width_override = height_override = 512

        image = Image.open(BytesIO(self.as_bytes()))

        width, height = image.size
        min_dimension = min(width, height)
        left = int((width - min_dimension) / 2)
        top = int((height - min_dimension) / 2)
        right = int((width + min_dimension) / 2)
        bottom = int((height + min_dimension) / 2)

        image.crop((left, top, right, bottom)).resize(
            (width_override, height_override),
            Image.Resampling.LANCZOS,
        ).save(self.local_path, "WEBP")

        return self.local_path

    @property
    def local_path(self) -> Path:
        """The path which the is/would be stored at locally.

        Returns:
            Path: where the file is/will be stored
        """
        if not hasattr(self, "_local_path"):
            self._local_path = PHOTOS_DIRECTORY / self.filename

        return self._local_path


class CosmoImageDownloader(Hass):  # type: ignore[misc]
    """Monitors the Cosmo vacuum's cleaning history."""

    album: Album
    client: GooglePhotosClient

    def initialize(self) -> None:
        """Initialize the app."""
        if not PHOTOS_DIRECTORY.is_dir():
            PHOTOS_DIRECTORY.mkdir(parents=True)

        self.client = GooglePhotosClient(
            client_id=self.args["client_id"],
            client_secret=self.args["client_secret"],
            creds_cache_dir=Path("/homeassistant/.wg-utilities/oauth_credentials"),
            use_existing_credentials_only=True,
        )

        self.run_every(self.get_latest_photo, "now", 60 * 60)

    def refresh_album(self) -> None:
        """Refresh the album from the Google Photos API."""
        self.album = self.client.get_album_by_id(self.args["album_id"])

    def get_latest_photo(self, _: dict[str, Any] | None = None) -> None:
        """Get the latest photo from the album."""
        self.refresh_album()

        if missing_count := self.album.media_items_count - len(
            list(PHOTOS_DIRECTORY.rglob("*.py")),
        ):
            self.log("Missing %i photos, downloading...", missing_count)

            for photo in self.album.media_items:
                if photo.media_type != MediaType.IMAGE:
                    continue

                webp = WebPImage.model_validate(
                    {"google_client": self.client, **photo.model_dump()},
                )

                if not webp.is_downloaded:
                    webp.download()
                    self.log(
                        "%s %ssuccessfully downloaded to %s",
                        webp.filename,
                        "" if webp.is_downloaded else "un",
                        webp.local_path,
                    )
