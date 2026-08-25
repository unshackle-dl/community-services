from __future__ import annotations

import re
from http.cookiejar import CookieJar
from typing import Any

import click
from click import Context
from langcodes import Language
from unshackle.core.credential import Credential
from unshackle.core.manifests import HLS
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapter, Chapters, Tracks
from unshackle.core.tracks.attachment import Attachment


class WFUK(Service):
    """
    Service code for WatchFree UK streaming service (https://www.watchfreeuk.com).

    Version: 1.0.0
    Author: @sp4rk.y
    Date: 2025-11-27
    Authorization: None (Free service)

    Robustness:
        AES-128: 1080p, AAC2.0

    Tips:
        - Input can be a URL or direct ID:
            SERIES: https://www.watchfreeuk.co.uk/shows/971809f7-ce40-11ed-bdce-06f38b7de9d9/bloodline-detectives
            MOVIE: https://www.watchfreeuk.co.uk/watch/vod/38285233/2021-war-of-the-worlds
        - No authentication required - service is free

    Notes:
        - Service is geofenced to GB only
        - Content is HLS with AES-128 encryption (no Widevine DRM)
        - Streams are provided via SimpleStream CDN
    """

    TITLE_RE = (
        r"^(?:https?://(?:www\.)?watchfreeuk\.co\.uk/)?"
        r"(?:(?P<type>shows|watch/vod)/)?"
        r"(?P<id>[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|\d+)"
        r"(?:/[^/]+)?$"
    )
    GEOFENCE = ("gb",)

    @staticmethod
    @click.command(name="WFUK", short_help="https://www.watchfreeuk.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> WFUK:
        return WFUK(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.metadata_url = self.config["endpoints"]["metadata"]
        self.streams_url = self.config["endpoints"]["streams"]
        self.api_key = self.config["api_key"]

        self.params = {
            "platform": self.config["params"]["platform"],
            "key": self.api_key,
            "cc": self.config["params"]["cc"],
            "lang": self.config["params"]["lang"],
            "region": self.config["params"]["region"],
        }

    def authenticate(
        self,
        cookies: CookieJar | None = None,
        credential: Credential | None = None,
    ) -> None:
        """No authentication required for this free service."""
        super().authenticate(cookies, credential)

    def get_titles(self) -> Movies | Series:
        """Get titles from WatchFree UK."""
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Invalid title format: {self.title}")

        title_id = match.group("id")
        title_type = match.group("type")

        if title_type == "shows" or (title_type is None and "-" in title_id):
            r = self.session.get(
                f"{self.metadata_url}/series/{title_id}", params=self.params
            )
            r.raise_for_status()

            data = r.json()
            series_data = data.get("response", {}).get("series", {})
            series_title = series_data.get("title", "Unknown")

            episodes = []
            for season in series_data.get("seasons", []):
                season_num = int(season.get("title", 0))

                for tile in season.get("tiles", []):
                    if tile.get("type") != "show":
                        continue

                    episodes.append(
                        Episode(
                            id_=tile.get("id") or tile.get("uvid"),
                            service=self.__class__,
                            title=series_title,
                            season=season_num,
                            number=int(tile.get("episode", 0)),
                            name=tile.get("title", f"Episode {tile.get('episode', 0)}"),
                            year=None,
                            language="en",
                            data=tile,
                        )
                    )

            return Series(episodes)

        r = self.session.get(f"{self.metadata_url}/show/{title_id}", params=self.params)
        r.raise_for_status()

        data = r.json()
        show_data = data.get("response", {}).get("show", {})

        return Movies(
            [
                Movie(
                    id_=show_data.get("id"),
                    service=self.__class__,
                    name=show_data.get("title"),
                    year=show_data.get("release_year"),
                    language="en",
                    data=show_data,
                )
            ]
        )

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        """Get tracks for a title."""
        stream_params = {
            **self.params,
            "build_number": "5540068",
            "gdpr_consent": "",
        }

        r = self.session.get(
            f"{self.streams_url}/show/stream/{title.id}", params=stream_params
        )
        r.raise_for_status()

        data = r.json()
        stream_data = data.get("response", {})

        manifest_url = stream_data.get("stream")
        if not manifest_url:
            raise ValueError(f"No stream URL found for title: {title.id}")

        if "ads" in manifest_url.lower() or stream_data.get("ads"):
            self.log.warning(
                "Content may have pre-roll ads - main content should follow"
            )

        tracks = HLS.from_url(url=manifest_url, session=self.session).to_tracks(
            language=title.language
        )

        if isinstance(title, Movie):
            if "image" in title.data:
                tracks.add(
                    Attachment.from_url(url=title.data["image"], name=f"{title.name}")
                )
        else:
            if "image" in title.data:
                tracks.add(
                    Attachment.from_url(
                        url=title.data["image"],
                        name=f"{title.title} - S{title.season:02d}E{title.number:02d}",
                    )
                )

        for track in tracks.audio + tracks.subtitles:
            if track.language:
                lang_obj = Language.get(track.language)
                lang_display = lang_obj.display_name()
                if not track.name:
                    track.name = lang_display
                elif lang_display not in track.name:
                    track.name = f"{lang_display} {track.name}"

        return tracks

    def get_chapters(self, title: Movie | Episode) -> Chapters:
        """Get chapters for a title using playnext_timecode for credits marker."""
        chapters = []
        playnext = title.data.get("playnext_timecode")
        if playnext:
            try:
                parts = playnext.split(":")
                if len(parts) == 3:
                    hours, minutes, seconds = map(int, parts)
                    credits_time = hours * 3600 + minutes * 60 + seconds
                    chapters.append(
                        Chapter(name="Credits", timestamp=float(credits_time))
                    )
            except (ValueError, AttributeError):
                pass

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        """No Widevine DRM for this service."""
        return None

    def get_widevine_license(self, *, challenge: bytes, **_: Any) -> bytes | None:
        """No Widevine DRM for this service."""
        return None
