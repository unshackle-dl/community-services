from __future__ import annotations

import base64
import hashlib
import json
import re
import warnings
from http.cookiejar import MozillaCookieJar
from typing import Any

import click
from bs4 import XMLParsedAsHTMLWarning
from click import Context
from langcodes import Language
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH, HLS
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series
from unshackle.core.tracks import Audio, Chapters, Subtitle, Tracks, Video
from unshackle.core.utils.collections import as_list

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


class BritBox(Service):
    """
    Service code for BritBox streaming service (https://www.britbox.com).
    Originally based on BBC iPlayer code for Devine by stabbedbybrick.

    Version: 1.0.0
    Author: stabbedbybrick (modified by ST02)
    Date: 2025-10-24
    Authorization: Credentials

    Robustness: N/A (Content is DRM-free)

    Tips:
        - Use full title URL as input for best results
            https://www.britbox.com/{geo}/show/{id}
            https://www.britbox.com/{geo}/movie/{id}
            https://www.britbox.com/{geo}/episode/{id}
        - Use --list-titles before downloading, listings can be inconsistent
        - Use --range HLG to request H.265 UHD tracks

    Notes:
        - Service supports multiple regions (US, CA, AU, DK, FI, SE, NO, ZA)
        - Region is automatically detected from URL ({geo} parameter)
        - Content is DRM-free and does not require CDM
        - H.265/HEVC tracks are available in HLG format for UHD content
    """

    ALIASES = ()
    GEOFENCE = ()  # "ca", "us", "au", "dk", "fi", "se", "no", "za")
    TITLE_RE = r"^(?:https?://(?:www\.)?britbox\.com/)(?P<geo>\w{2})/(?P<kind>show|movie|episode)/(?P<id>[a-zA-Z0-9_()-]+)(?:/.*)?$"

    @staticmethod
    @click.command(name="BritBox", short_help="https://www.britbox.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> BritBox:
        return BritBox(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        # self.track_request is set by Service.__init__() from CLI params
        self.vcodec = (
            self.track_request.codecs[0] if self.track_request.codecs else None
        )
        self.range = self.track_request.ranges

        if self.range[0].name == "HLG":
            self.vcodec = "H.265"

    def authenticate(
        self,
        cookies: MozillaCookieJar | None = None,
        credential: Credential | None = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise OSError("Service requires Credentials for Authentication.")

        cache = self.cache.get(f"tokens_{credential.sha1}")

        if cache and not cache.expired:
            # cached
            self.log.info(" + Using cached Tokens...")
            tokens = cache.data
        elif cache and cache.expired:
            # expired, refresh
            self.log.info("Refreshing cached Tokens")
            r = self.session.post(
                self.config["endpoints"]["authorization"],
                headers={
                    "User-Agent": self.config["headers"]["user_agent"],
                    "Host": self.config["headers"]["host_data"],
                },
                json={
                    "deviceName": self.config["DEVICE_NAME"],
                    "email": credential.username,
                    "id": self.config["DEVICE_ID"],
                    "password": credential.password,
                    "scopes": ["Catalog", "Settings"],
                },
            )
            try:
                res = r.json()
            except json.JSONDecodeError:
                raise ValueError(f"Failed to refresh tokens: {r.text}") from None

            if "error" in res:
                raise ConnectionError(
                    f"Failed to refresh tokens: {res.get('errorMessage', 'Unknown error')}"
                )

            tokens = res[0]["value"]
            self.log.info(" + Refreshed")
        else:
            # new
            json_data = {
                "deviceName": self.config["DEVICE_NAME"],
                "email": credential.username,
                "id": self.config["DEVICE_ID"],
                "password": credential.password,
                "scopes": ["Catalog", "Settings"],
            }
            r = self.session.post(
                self.config["endpoints"]["authorization"],
                headers={
                    "User-Agent": self.config["headers"]["user_agent"],
                    "Host": self.config["headers"]["host_data"],
                },
                json=json_data,
            )
            try:
                res = r.json()
            except json.JSONDecodeError:
                raise ValueError(f"Failed to log in: {r.text}") from None

            if "error" in res:
                raise ConnectionError(
                    f"Failed to log in: {res.get('errorMessage', 'Unknown error')}"
                )

            tokens = res[0]["value"]
            self.log.info(" + Acquired tokens...")

        exp = json.loads(base64.b64decode(tokens.split(".")[1] + "==").decode("utf-8"))[
            "exp"
        ]
        cache.set(tokens, expiration=exp)

        self.bearer = tokens

    def get_titles(self) -> Movies | Series:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(
                "Invalid URL format. Expected: https://www.britbox.com/{geo}/{kind}/{id}"
            )

        self.geo, kind, pid = (match.group(i) for i in ("geo", "kind", "id"))
        if not pid:
            raise ValueError("Unable to parse title ID - is the URL or id correct?")

        self.log.info(f"Fetching {kind} with ID: {pid} (region: {self.geo})")

        data = self.get_data(pid=pid, cpid=None, kind=kind)
        if kind == "episode":
            return self.get_single_episode(pid, kind)
        elif data is None:
            raise ValueError(
                f"Metadata was not found - if {pid} is an episode, use full URL as input"
            )

        if kind == "movie":
            return Movies(
                [
                    Movie(
                        id_=data["item"]["id"],
                        name=data["item"]["title"],
                        year=data["item"]["releaseYear"],
                        service=self.__class__,
                        language="en",
                        data=data,
                    )
                ]
            )
        else:
            seasons = [
                self.get_data(pid=None, cpid=x["path"].split("/")[-1], kind="season")
                for x in data["item"]["show"]["seasons"]["items"]
            ]
            episodes = [
                self.create_episode(episode, season)
                for season in seasons
                for episode in season["item"]["episodes"]["items"]
            ]
            return Series(episodes)

    def get_tracks(self, title: Movie | Episode) -> Tracks:
        self.log.info(
            f"Fetching tracks for: {title.name if hasattr(title, 'name') else title.title}"
        )

        # Get available media versions
        media = self.check_all_versions(title.id)

        if not media:
            raise ConnectionError(
                "No media found. If you're behind a VPN/proxy, you might be blocked"
            )

        # Select the appropriate video connection based on codec preference
        connection = {}
        for video in [x for x in media if x["kind"] == "video"]:
            connections = sorted(
                video["connection"], key=lambda x: x["dpw"], reverse=True
            )
            if self.vcodec == "H.265":
                # For H.265/UHD, use highest quality connection
                connection = connections[0]
                self.log.debug(
                    f"Selected H.265 connection: {connection.get('supplier', 'unknown')}"
                )
            else:
                # For H.264, prefer DASH from Akamai
                connection = next(
                    x
                    for x in connections
                    if x["supplier"] == "mf_akamai" and x["transferFormat"] == "dash"
                )
                self.log.debug("Selected H.264 DASH connection from Akamai")

            break

        # Convert DASH to HLS for H.264 streams
        if self.vcodec != "H.265":
            if connection["transferFormat"] == "dash":
                connection["href"] = "/".join(
                    [
                        *connection["href"]
                        .replace("dash", "hls")
                        .replace(".hlsv2.ism", "")
                        .split("?")[0]
                        .split("/")[0:-1],
                        "hls",
                        "master.m3u8",
                    ]
                )
                connection["transferFormat"] = "hls"
                self.log.debug("Converted DASH to HLS manifest URL")
            elif connection["transferFormat"] == "hls":
                connection["href"] = "/".join(
                    [
                        *connection["href"]
                        .replace(".hlsv2.ism", "")
                        .split("?")[0]
                        .split("/")[0:-1],
                        "hls",
                        "master.m3u8",
                    ]
                )
                self.log.debug("Updated HLS manifest URL")

            if connection["transferFormat"] != "hls":
                raise ValueError(
                    f"Unsupported video media transfer format {connection['transferFormat']!r}"
                )

        if connection["transferFormat"] == "dash":
            tracks = DASH.from_url(
                url=connection["href"], session=self.session
            ).to_tracks(language=title.language)
        elif connection["transferFormat"] == "hls":
            tracks = HLS.from_url(
                url=connection["href"], session=self.session
            ).to_tracks(language=title.language)
        else:
            raise ValueError(
                f"Unsupported video media transfer format {connection['transferFormat']!r}"
            )

        for video in tracks.videos:
            # UHD DASH manifest has no range information, so we add it manually
            if video.codec == Video.Codec.HEVC:
                video.range = Video.Range.HLG

            if any(re.search(r"-audio_\w+=\d+", x) for x in as_list(video.url)):
                # create audio stream from the video stream
                audio_url = re.sub(r"-video=\d+", "", as_list(video.url)[0])
                audio = Audio(
                    # use audio_url not video url, as to ignore video bitrate in ID
                    id_=hashlib.md5(audio_url.encode()).hexdigest()[0:7],
                    url=audio_url,
                    codec=Audio.Codec.from_codecs(
                        video.data["hls"]["playlist"].stream_info.codecs.split(",")[0]
                    ),
                    language=video.data["hls"]["playlist"].media[0].language,
                    bitrate=int(
                        self.find(r"-audio_\w+=(\d+)", as_list(video.url)[0]) or 0
                    ),
                    channels=video.data["hls"]["playlist"].media[0].channels,
                    descriptive=False,  # Not available
                    descriptor=Audio.Descriptor.HLS,
                    drm=video.drm,
                    data=video.data,
                )
                if not tracks.exists(by_id=audio.id):
                    # some video streams use the same audio, so natural dupes exist
                    tracks.add(audio)
                # remove audio from the video stream
                video.url = next(
                    re.sub(r"-audio_\w+=\d+", "", x) for x in as_list(video.url)
                )
                video.codec = Video.Codec.from_codecs(
                    video.data["hls"]["playlist"].stream_info.codecs
                )
                video.bitrate = int(
                    self.find(r"-video=(\d+)", as_list(video.url)[0]) or 0
                )

        for caption in [x for x in media if x["kind"] == "captions"]:
            connection = caption["connection"][0]
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(connection["href"].encode()).hexdigest()[0:6],
                    url=connection["href"],
                    codec=Subtitle.Codec.from_codecs("ttml"),
                    language=caption["language"],  # title.language,
                    is_original_lang=str(title.language) in caption["language"],
                    forced=False,
                    sdh=caption["purpose"] == "hard-of-hearing",
                )
            )

        # Apply unified language display names to all audio and subtitle tracks
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
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        """BritBox content is DRM-free and does not require Widevine certificates."""
        self.log.debug("BritBox content is DRM-free - no certificate needed")
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        """BritBox content is DRM-free and does not require Widevine licenses."""
        self.log.debug("BritBox content is DRM-free - no license needed")
        return None

    # service specific functions

    def get_data(self, pid: str | None, cpid: str | None, kind: str) -> dict:
        if cpid is None:
            params = {
                "path": f"/{kind}/{pid}",
                "useCustomId": "true",
                "listPageSize": "100",
                "maxListPrefetch": "15",
                "itemDetailExpand": "all",
                "textEntryFormat": "html",
                "device": "web_browser",
                "sub": "Subscriber",
                "segments": self.geo,
            }

            contentid = self.session.get(
                self.config["endpoints"]["content_page"],
                params=params,
                headers={"Referer": self.config["headers"]["referer"]},
            )

            contentid.raise_for_status()

            contentid = contentid.json()["externalResponse"]["entries"][0]["item"]["id"]

        params = {
            "path": f"/{kind}/{pid.replace(pid.split('_')[-1], contentid) if cpid is None else cpid}",
            "list_page_size_large": "100",
            "item_detail_expand": "all",
            "item_detail_select_season": "first",
            "related_items_count": "false",
            "device": "tv_android",
            "sub": "Subscriber",
            "segments": [self.geo.upper(), "supportTA"],
            "ff": ["ldp", "idp"],
            "lang": f"en-{self.geo.upper()}",
            "c": "tv_firetv",
            "v": "1.0.0",
        }

        r = self.session.get(
            self.config["endpoints"]["metadata"],
            headers={
                "User-Agent": self.config["headers"]["user_agent"],
                "Host": self.config["headers"]["host_data"],
            },
            params=params,
        )
        r.raise_for_status()

        return r.json()

    def get_token(self, id_: str) -> tuple[str, str]:
        params = {
            "delivery": "stream",
            "resolution": "HD-1080",
            "device": "tv_android",
            "sub": "Subscriber",
            "segments": self.geo.upper(),
            "ff": ["ldp", "idp"],
            "lang": f"en-{self.geo.upper()}",
        }

        metadata = self.session.get(
            url=self.config["endpoints"]["token"].format(id_=id_),
            headers={
                "User-Agent": self.config["headers"]["user_agent"],
                "Host": self.config["headers"]["host_data"],
                "Authorization": f"Bearer {self.bearer}",
            },
            params=params,
        ).json()

        return metadata[0]["token"], metadata[0]["name"]

    def check_all_versions(self, vpid: str) -> list:
        token, vpid = self.get_token(vpid)
        url = self.config["endpoints"]["manifest"].format(
            vpid=vpid,
            mediaset="iptv-uhd" if self.vcodec == "H.265" else "iptv-all",
        )

        session = self.session
        manifest = session.get(
            url,
            headers={
                "User-Agent": self.config["headers"]["user_agent"],
                "Host": self.config["headers"]["host_stream"],
                "Authorization": f"britbox x={token}",
            },
        ).json()

        if "result" in manifest:
            return []

        return manifest["media"]

    def create_episode(self, episode: dict, season: dict) -> Episode:
        title = episode["showTitle"]
        season_num = int(season["item"]["seasonNumber"])
        ep_num = int(episode["episodeNumber"])
        ep_name = episode["episodeName"]

        return Episode(
            id_=episode["id"],
            service=self.__class__,
            title=title,
            season=season_num,
            number=ep_num,
            name=ep_name,
            language="en",
            data=episode,
        )

    def get_single_episode(self, pid: str, kind: str) -> Series:
        params = {
            "path": f"/{kind}/{pid}",
            "useCustomId": "true",
            "listPageSize": "100",
            "maxListPrefetch": "15",
            "itemDetailExpand": "all",
            "textEntryFormat": "html",
            "device": "web_browser",
            "sub": "Subscriber",
            "segments": self.geo,
        }

        # Fetch content ID (not used but needed for API consistency)
        self.session.get(
            self.config["endpoints"]["content_page"],
            params=params,
            headers={
                "User-Agent": self.config["headers"]["user_agent_web"],
                "Referer": self.config["headers"]["referer"],
            },
        ).json()["externalResponse"]["entries"][0]["item"]["id"]

        params = {
            "path": f"/{kind}/{pid}",
            "list_page_size_large": "100",
            "item_detail_expand": "all",
            "item_detail_select_season": "first",
            "related_items_count": "false",
            "device": "tv_android",
            "sub": "Subscriber",
            "segments": [self.geo.upper(), "supportTA"],
            "ff": ["ldp", "idp"],
            "lang": f"en-{self.geo.upper()}",
            "c": "tv_firetv",
            "v": "1.0.0",
        }
        r = self.session.get(
            self.config["endpoints"]["metadata"],
            headers={
                "User-Agent": self.config["headers"]["user_agent"],
                "Host": self.config["headers"]["host_data"],
            },
            params=params,
        )
        r.raise_for_status()

        data = json.loads(r.content)

        season = int(data["item"]["season"]["seasonNumber"])
        number = int(data["item"]["episodeNumber"])
        name = data["item"]["episodeName"]

        return Series(
            [
                Episode(
                    id_=data["item"]["id"],
                    service=self.__class__,
                    title=data["item"]["showTitle"],
                    season=season,
                    number=number,
                    name=name,
                    language="en",
                )
            ]
        )

    def find(self, pattern: str, string: str, group: int | None = None) -> str | None:
        if group:
            m = re.search(pattern, string)
            if m:
                return m.group(group)
        else:
            return next(iter(re.findall(pattern, string)), None)
