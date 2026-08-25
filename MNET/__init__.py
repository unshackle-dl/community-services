from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import click
from langcodes import Language
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import HLS
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapters, Subtitle, Tracks
from unshackle.core.utilities import is_close_match

Entry = tuple[dict[str, Any], int, int | None]


class MNET(Service):
    """
    Service code for MNET Plus (https://www.mnetplus.world).

    Version: 1.0.0
    Author: @sp4rk.y
    Authorization: Credentials
    Robustness:
        DRM-free HLS (CloudFront signed cookies): 2160p

    Tips:
        - Input can be a show URL, a single video URL, or the bare 24-character ID:
            https://www.mnetplus.world/contents/en/shows/694ab2f06ebbb103c8f53622/videos
            https://www.mnetplus.world/media/en/videos/69687821b1f8c402f53f5781
        - 2160p is only offered on some titles and needs a subscription; the API bakes the
          entitled resolution cap into the manifest URL, so an unentitled account silently
          gets a 1080p master.
        - Paid (TVOD) videos are hidden by default because an unpurchased one serves a short
          preview instead of the episode. Pass --tvod to list them anyway.

    Notes:
        - No DRM. Segments are gated by short-lived CloudFront signed cookies fetched per
          video, refreshed mid-download for long titles.
        - Episodes often ship in numbered parts (EP. 1-1, EP. 1-2), mapped onto Episode.part.
        - A video carries one of two subtitle sources, never both: WebVTT renditions inside
          the manifest, or a caption API that serves timed cues. The caption API flags its
          machine-translated languages, and those become "<Language> - AI" track names.
    """

    ALIASES = ("MNETPLUS", "MNET+")
    TITLE_RE = r"^(?:https?://(?:www\.)?mnetplus\.world/(?:contents/\w+/shows/|media/\w+/videos/))?(?P<id>[a-f0-9]{24})"

    ORIGINAL_LANGUAGE = "ko"
    SUBTITLE_LANGUAGE_MAP: ClassVar[dict[str, str]] = {
        "zh": "zh-Hans",
        "za": "zh-Hant",
        "zh-CN": "zh-Hans",
        "zh-TW": "zh-Hant",
    }
    CUE_WINDOW = 60

    # a show can switch naming language mid-run: "STEAL HEART CLUB EP.4" then "스틸하트클럽 5회"
    EPISODE_RE = re.compile(
        r"(?:\bEP\.?\s*|\[Full VOD\s+)(\d+)(?:\s*-\s*(\d+))?|(\d+)\s*회", re.IGNORECASE
    )
    FINAL_RE = re.compile(r"\bFINAL\b|\[Last Episode\]", re.IGNORECASE)
    PART_RE = re.compile(r"\bPart\s*(\d+)", re.IGNORECASE)

    @staticmethod
    @click.command(name="MNET", short_help="https://www.mnetplus.world", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "--tvod", is_flag=True, default=False, help="Include paid (TVOD) videos."
    )
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: Any) -> MNET:
        return MNET(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str, tvod: bool):
        self.title = title
        self.tvod = tvod
        super().__init__(ctx)
        if self.config is None:
            raise OSError("config.yaml is missing for this service.")

        self.cdn_video_id: str | None = None
        self.cdn_cookies_expire = 0.0

    def authenticate(
        self,
        cookies: CookieJar | None = None,
        credential: Credential | None = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise OSError("Service requires Credentials (email and password).")

        self.session.headers.update(self.config["headers"])

        cache = self.cache.get(f"token_{credential.sha1}")
        if cache.expired or not cache.data:
            token = self.session.post(
                url=self.config["endpoints"]["login"],
                json={
                    "email": credential.username,
                    "password": credential.password,
                    "autoLogin": False,
                },
            ).json()
            if not token.get("accessToken"):
                raise ValueError(f"Login failed: {token.get('message') or token}")
            token["device_id"] = (cache.data or {}).get("device_id") or str(uuid4())
            cache.set(data=token, expiration=token.get("expiresIn", 86400))
        else:
            self.log.info(" + Using cached token")

        self.session.headers.update(
            {
                "Authorization": f"Bearer {cache.data['accessToken']}",
                "X-User-Agent": f"en:US::WEB:Chrome:::{cache.data['device_id']}",
            }
        )

    def search(self) -> Generator[SearchResult, None, None]:
        for show in self.session.get(
            url=self.config["endpoints"]["media_events"]
        ).json()["mediaEvents"]:
            if self.title.lower() in show["name"].lower():
                yield SearchResult(
                    id_=show["mediaEventId"],
                    title=show["name"],
                    description=show.get("description"),
                    label=show.get("mediaEventType"),
                    url=f"https://www.mnetplus.world/contents/en/shows/{show['mediaEventId']}/videos",
                )

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(
                "Could not parse an ID from the title - is the URL correct?"
            )
        title_id = match.group("id")

        show = (
            {}
            if "/media/" in self.title
            else self.session.get(
                url=self.config["endpoints"]["show"].format(show_id=title_id)
            ).json()
        )
        if show.get("mediaEventId"):
            videos = list(self.get_show_videos(title_id))
        else:
            videos = [self.get_video(title_id)]
            show = videos[0].get("mediaEvent") or videos[0]

        videos = self.filter_videos(videos)
        videos.sort(key=lambda v: (v.get("startAt") or "", v["name"]))
        episodes, dated = self.number_episodes(videos)
        if len(episodes) != len(videos):
            self.log.info(
                f" - Skipping {len(videos) - len(episodes)} video(s) with no episode number (e.g. trailers)"
            )

        return Series(
            [
                Episode(
                    id_=video["videoId"],
                    service=self.__class__,
                    title=show["name"],
                    season=1,
                    number=number,
                    part=part,
                    air_date=self.air_date(video) if dated else None,
                    name=self.clean_name(video["name"], show["name"]),
                    year=(video.get("startAt") or "")[:4] or None,
                    language=self.ORIGINAL_LANGUAGE,
                    data=video,
                )
                for video, number, part in episodes
            ]
        )

    def get_tracks(self, title: Title_T) -> Tracks:
        video = self.get_video(title.id)

        product = video.get("productInfo") or {}
        if product and not product.get("isPurchased"):
            raise ValueError(
                f"This {product.get('type', 'paid')} title has not been purchased - "
                "the service only serves a short preview stream for it."
            )

        self.set_cdn_cookies(title.id)
        tracks = HLS.from_url(url=video["videoUrl"], session=self.session).to_tracks(
            language=self.ORIGINAL_LANGUAGE
        )

        for audio in tracks.audio:
            audio.language = Language.get(self.ORIGINAL_LANGUAGE)
            audio.is_original_lang = True
        for subtitle in tracks.subtitles:
            subtitle.language = self.subtitle_language(str(subtitle.language))
        tracks.add(list(self.get_caption_tracks(title.id, video)))

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def on_segment_downloaded(self, track: AnyTrack, segment: Path) -> None:
        if self.cdn_video_id and time.monotonic() >= self.cdn_cookies_expire:
            self.set_cdn_cookies(self.cdn_video_id)

    def get_video(self, video_id: str) -> dict[str, Any]:
        video = self.session.get(
            url=self.config["endpoints"]["video"].format(video_id=video_id)
        ).json()
        if not video.get("videoUrl"):
            raise ValueError(
                f"Video {video_id} is unavailable: {video.get('message') or video}"
            )
        if video.get("playerType") != "ORIGINAL":
            raise ValueError(
                f"This video is hosted on {video['playerType'].title()}, not on MNET Plus: {video['videoUrl']}"
            )
        if (video.get("geoBlock") or {}).get("isBlocked"):
            raise ValueError(
                video["geoBlock"].get("blockedMessage") or "This video is geo-blocked."
            )
        return video

    def get_show_videos(self, show_id: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "searchId": show_id,
            "filterChipType": "MEDIA_EVENT_VOD",
            "size": 100,
        }
        while True:
            page = self.session.get(
                url=self.config["endpoints"]["archives"], params=params
            ).json()
            yield from page["content"]
            if not page.get("hasNext"):
                return
            params["cursor"] = page["endCursor"]

    def filter_videos(self, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop videos hosted off-service (e.g. YouTube) and, unless --tvod, paid ones."""
        hosted = [v for v in videos if v.get("playerType") in (None, "ORIGINAL")]
        if len(hosted) != len(videos):
            self.log.info(
                f" - Skipping {len(videos) - len(hosted)} video(s) hosted off-service (e.g. YouTube)"
            )
        if self.tvod:
            return hosted
        free = [v for v in hosted if v.get("isFree", True)]
        if len(free) != len(hosted):
            self.log.info(
                f" - Skipping {len(hosted) - len(free)} paid (TVOD) video(s), pass --tvod to include them"
            )
        return free

    def set_cdn_cookies(self, video_id: str) -> None:
        signed = self.session.post(
            url=self.config["endpoints"]["cookies"].format(video_id=video_id)
        ).json()
        for pair in (signed["policy"], signed["signature"], signed["keyPairId"]):
            name, value = pair.split("=", 1)
            self.session.cookies.set(name, value, domain=self.config["cdn_domain"])
        self.cdn_video_id = video_id
        self.cdn_cookies_expire = time.monotonic() + int(
            signed["refreshIntervalSeconds"]
        )

    @staticmethod
    def air_date(video: dict[str, Any]) -> str | None:
        return (video.get("startAt") or "")[:10] or None

    @classmethod
    def number_episodes(cls, videos: list[dict[str, Any]]) -> tuple[list[Entry], bool]:
        """
        Read the episode and part number out of each video name, e.g. ``EP. 3-2``,
        ``[Full VOD 1-1]`` or ``5회``, then order the show by them. Drop any video whose
        name has no number: a show archive lists trailers and teasers next to the episodes,
        and it has no number field to sort or filter on. Two exceptions: an unnumbered
        finale (``FINAL``/``[Last Episode]``, per namu.wiki the last regular episode)
        continues the numbering, and when fewer than half the names carry a number the
        show is a variety/daily feed, numbered by position in release order and named by
        air date instead (the second return value).
        When one number covers several videos (a main episode plus its review, or
        ``EP.1 Part 1``/``Part 2`` splits), part them by release order within that number.
        """
        numbered: list[Entry] = []
        finals = []
        for video in videos:
            if match := cls.EPISODE_RE.search(video["name"]):
                numbered.append(
                    (
                        video,
                        int(match.group(1) or match.group(3)),
                        int(match.group(2) or 0) or None,
                    )
                )
            elif cls.FINAL_RE.search(video["name"]):
                finals.append(video)

        if len(numbered) * 2 < len(videos):
            days = Counter(map(cls.air_date, videos))
            seen: Counter[str | None] = Counter()
            positional: list[Entry] = []
            for i, video in enumerate(videos, 1):
                day = cls.air_date(video)
                seen[day] += 1
                positional.append(
                    (video, i, seen[day] if day and days[day] > 1 else None)
                )
            return positional, len(videos) > 1

        if numbered and finals:
            last = max(number for _, number, _ in numbered)
            numbered += [(video, last + 1, None) for video in finals]

        groups: dict[int, list[Entry]] = {}
        for entry in numbered:
            groups.setdefault(entry[1], []).append(entry)
        result: list[Entry] = []
        for number, group in groups.items():
            parts = [part for _, _, part in group]
            if len(group) == 1 or len(set(parts)) == len(group):
                result += group
                continue
            hints = [cls.PART_RE.search(video["name"]) for video, _, _ in group]
            if all(hints) and len({hint.group(1) for hint in hints if hint}) == len(
                group
            ):
                result += [
                    (video, number, int(hint.group(1)))
                    for (video, _, _), hint in zip(group, hints, strict=False)
                    if hint
                ]
            else:
                result += [
                    (video, number, i) for i, (video, _, _) in enumerate(group, 1)
                ]
        return sorted(result, key=lambda entry: (entry[1], entry[2] or 0)), False

    @classmethod
    def clean_name(cls, name: str, show: str) -> str | None:
        """
        Reduce ``(SUB) [Original] EP. 1-1 Where are we? | ON THE MAP`` to ``Where are we?``.
        The show name and the episode number are already carried by the Episode itself.
        """
        name = re.sub(
            rf"\s*\|\s*{re.escape(show.strip())}.*$",
            "",
            name.strip(),
            flags=re.IGNORECASE,
        )
        name = re.sub(r"^(?:\s*(?:\([^)]*\)|\[[^\]]*\]))+", "", name)
        name = cls.EPISODE_RE.sub("", name, count=1)
        return re.sub(r"\s{2,}", " ", name).strip(" |") or None

    # Subtitles

    @classmethod
    def subtitle_language(cls, code: str) -> Language:
        code = code.replace("_", "-")
        return Language.get(cls.SUBTITLE_LANGUAGE_MAP.get(code, code))

    def get_caption_tracks(
        self, video_id: str, video: dict[str, Any]
    ) -> Iterator[Subtitle]:
        """
        Build subtitles from the caption API. Only videos without in-manifest subtitle
        renditions carry a ``videoCaption``, and each language there is either human
        authored or machine translated.
        """
        caption = video.get("videoCaption")
        if not caption:
            return
        url = self.config["endpoints"]["cues"].format(
            video_id=video_id, caption_id=caption["videoCaptionId"]
        )
        duration = int((video.get("videoLength") or 0) / 1000)
        for config in caption["languageConfigs"]:
            language = self.subtitle_language(config["language"])
            yield Subtitle(
                id_=f"{caption['videoCaptionId']}-{config['language']}",
                url=url,
                codec=Subtitle.Codec.SubRip,
                language=language,
                is_original_lang=is_close_match(language, [self.ORIGINAL_LANGUAGE]),
                name=f"{config['languageLabel']} - AI"
                if config.get("aiGeneratedLabel")
                else None,
                downloader=self.cues_downloader(url, config["language"], duration),
            )

    def cues_downloader(self, url: str, language: str, duration: int) -> Any:
        """Track downloader that walks the caption windows and writes one SubRip file."""
        offsets = range(0, duration + self.CUE_WINDOW, self.CUE_WINDOW)

        def fetch(offset: int) -> dict[str, Any]:
            params: dict[str, Any] = {"language": language, "displaySecond": offset}
            response = self.session.get(url=url, params=params)
            response.raise_for_status()
            return response.json().get("contentMap") or {}

        def download(
            *, output_dir: Path, filename: str, **_: Any
        ) -> Generator[dict[str, Any], None, None]:
            yield {"total": len(offsets)}
            cues: dict[int, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for window in pool.map(fetch, offsets):
                    cues.update({int(second): cue for second, cue in window.items()})
                    yield {"advance": 1}
            save_path = output_dir / filename
            save_path.write_text(self.build_srt(cues), encoding="utf8")
            yield {"file_downloaded": save_path, "written": save_path.stat().st_size}

        return download

    @staticmethod
    def build_srt(cues: dict[int, dict[str, Any]]) -> str:
        def timestamp(seconds: int) -> str:
            return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02},000"

        blocks = []
        for index, second in enumerate(sorted(cues), 1):
            cue = cues[second]
            end = second + max(int(cue.get("displayDurationSecond") or 0), 1)
            blocks.append(
                f"{index}\n{timestamp(second)} --> {timestamp(end)}\n{cue['content'].strip()}\n"
            )
        return "\n".join(blocks)
