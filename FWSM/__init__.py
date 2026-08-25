import hashlib
import re
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import ClassVar

import click
from langcodes import Language
from unshackle.core.credential import Credential
from unshackle.core.manifests import HLS
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapter, Subtitle, Tracks


class FWSM(Service):
    """
    Service code for Fawesome streaming service (https://fawesome.tv/)

    Version: 1.0.0
    Author: @sp4rk.y
    Date: 2026-03-29
    Authorization: None

    Robustness:
        Widevine:
            L3: 1080p, AAC2.0

    Tips:
        - Input should be a complete URL or content ID:
            https://fawesome.tv/movies/10686321/the-pledge
            https://fawesome.tv/shows/100946/nash-bridges
            https://ifood.tv/shows/91654/southern-food-truck-wars
        - Movie/show type is auto-detected from URL path
        - Use -m/--movie flag when passing a raw ID for movies
    """

    ALIASES = ("iftv",)
    TITLE_RE = r"^(?:https?://(?:www\.)?(?:fawesome\.tv|ifood\.tv)/(?P<type>movies|shows|fawesome-topics)/)?(?P<id>\d+)"
    GEOFENCE = ()

    API_BASE = "https://rapi.ifood.tv"
    API_PARAMS: ClassVar[dict[str, str]] = {
        "appId": "7",
        "siteId": "1285",
        "auth-token": "1216525",
        "version": "sv6.0",
    }

    @staticmethod
    @click.command(name="FWSM", short_help="https://fawesome.tv")
    @click.argument("title", type=str)
    @click.option(
        "-m", "--movie", is_flag=True, default=False, help="Specify if it's a movie"
    )
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: any) -> "FWSM":
        return FWSM(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str, movie: bool) -> None:
        super().__init__(ctx)
        self.title = title
        self.movie = movie

    def authenticate(
        self,
        cookies: CookieJar | None = None,
        credential: Credential | None = None,
    ) -> None:
        pass

    def search(self) -> Generator[SearchResult, None, None]:
        results = self.session.get(
            url=f"{self.API_BASE}/recipes.php",
            params={
                **self.API_PARAMS,
                "searchType": "search",
                "keys": self.title,
            },
        ).json()

        for result in results.get("results", []):
            item_type = (result.get("item_type") or "movie").upper()
            yield SearchResult(
                id_=result.get("node_id", result["id"]),
                title=result["title"],
                description=(result.get("description") or "")[:120],
                label=item_type,
                url=result.get("url"),
            )

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(f"Could not parse title ID from: {self.title}")
        title_id = match.group("id")

        url_type = match.group("type")
        if url_type == "movies":
            self.movie = True
        elif url_type == "shows":
            self.movie = False

        if self.movie:
            return self.get_movie(title_id)
        return self.get_series(title_id)

    def get_movie(self, title_id: str) -> Movies:
        node_id = int(title_id)
        full_id = node_id if node_id > 200000000000000 else 200000000000000 + node_id

        info = self.session.get(
            url=f"{self.API_BASE}/recipeInfo.php",
            params={
                **self.API_PARAMS,
                "searchType": "nid",
                "nid": str(full_id),
            },
        ).json()

        node_data = info["node_data"]
        video_id = node_data["video_id"]
        mp4_url = node_data.get("video_url") or node_data.get("video_flv_url") or ""
        m3u8_url = self.construct_m3u8_url(mp4_url, video_id)
        search_data = self.search_by_title(node_data["title"], video_id)

        return Movies(
            [
                Movie(
                    id_=str(video_id),
                    service=self.__class__,
                    name=node_data["title"],
                    description=node_data.get("field_recipe_description_value") or "",
                    year=int(search_data["release_year"])
                    if search_data.get("release_year")
                    else None,
                    language=Language.get(
                        search_data.get("content_language_iso2") or "en"
                    ),
                    data={
                        "video_url": m3u8_url,
                        "cc_path": search_data.get("cc_path") or "",
                        "intro_st": search_data.get("intro_st"),
                        "intro_et": search_data.get("intro_et"),
                        "endcredit_st": search_data.get("endcredit_st"),
                        "endcredit_et": search_data.get("endcredit_et"),
                    },
                )
            ]
        )

    def search_by_title(self, title_name: str, node_id: int) -> dict:
        """Search by title name and find matching result by node_id."""
        results = self.session.get(
            url=f"{self.API_BASE}/recipes.php",
            params={
                **self.API_PARAMS,
                "searchType": "search",
                "keys": title_name,
            },
        ).json()

        for result in results.get("results", []):
            if int(result.get("node_id", 0)) == node_id:
                return result

        return {}

    def get_series(self, show_key: str) -> Series:
        shows = self.session.get(
            url=f"{self.API_BASE}/shows.php",
            params={
                **self.API_PARAMS,
                "searchType": "listoflist",
                "keys": show_key,
            },
        ).json()

        episodes = []
        channels = shows.get("channels", {})

        for season_num, season_info in channels.items():
            feed_url = season_info.get("feed", "")
            if not feed_url:
                continue

            season_episodes = self.fetch_all_episodes(feed_url)
            series_name = ""

            for ep in season_episodes:
                if not series_name:
                    series_name = ep.get("series_name") or ""

                node_id = ep.get("node_id") or ep["id"]
                video_url = ep.get("video_url") or ""

                if not video_url.endswith(".m3u8"):
                    video_url = self.construct_m3u8_url(video_url, int(node_id))

                ep_name = self.parse_episode_name(ep.get("title", ""), series_name)

                episodes.append(
                    Episode(
                        id_=str(node_id),
                        service=self.__class__,
                        title=series_name,
                        season=int(ep.get("season") or season_num),
                        number=int(ep.get("episode") or 0),
                        name=ep_name,
                        description=ep.get("description") or "",
                        year=int(ep["release_year"])
                        if ep.get("release_year")
                        else None,
                        language=Language.get(ep.get("content_language_iso2") or "en"),
                        data={
                            "video_url": video_url,
                            "cc_path": ep.get("cc_path") or "",
                            "intro_st": ep.get("intro_st"),
                            "intro_et": ep.get("intro_et"),
                            "endcredit_st": ep.get("endcredit_st"),
                            "endcredit_et": ep.get("endcredit_et"),
                        },
                    )
                )

        # Fawesome has inconsistent release_year (some episodes show upload year).
        # Use the minimum year across all episodes as the true original air date.
        years = [e.year for e in episodes if e.year]
        if years:
            original_year = min(years)
            for ep in episodes:
                ep.year = original_year

        return Series(episodes)

    def fetch_all_episodes(self, feed_url: str) -> list[dict]:
        """Fetch all episodes from a season feed URL."""
        separator = "&" if "?" in feed_url else "?"
        url = f"{feed_url}{separator}max-results=200"
        return self.session.get(url=url).json().get("results", [])

    @staticmethod
    def parse_episode_name(full_title: str, series_name: str) -> str:
        """Extract episode name from formatted title like 'S06 E01 - Rock and a Hard Place - Nash Bridges'."""
        name = re.sub(r"^S\d+ E\d+ - ", "", full_title)
        if series_name:
            name = re.sub(r"\s*-\s*" + re.escape(series_name) + r"$", "", name)
        return name

    def get_tracks(self, title: Title_T) -> Tracks:
        video_url = title.data["video_url"]

        tracks = HLS.from_url(
            url=video_url,
            session=self.session,
        ).to_tracks(title.language)

        cc_path = title.data.get("cc_path") or ""
        if cc_path:
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(cc_path.encode()).hexdigest()[:6],
                    url=cc_path,
                    codec=Subtitle.Codec.SubRip,
                    language=title.language,
                    sdh=True,
                )
            )

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        chapters: list[Chapter] = []

        intro_st = title.data.get("intro_st")
        intro_et = title.data.get("intro_et")
        if intro_st and intro_et and int(intro_st) > 0:
            chapters.extend(
                (
                    Chapter(timestamp=int(intro_st) * 1000, name="Intro"),
                    Chapter(timestamp=int(intro_et) * 1000),
                )
            )

        endcredit_st = title.data.get("endcredit_st")
        if endcredit_st and int(endcredit_st) > 0:
            chapters.append(Chapter(timestamp=int(endcredit_st) * 1000, name="Credits"))

        return chapters

    @staticmethod
    def construct_m3u8_url(video_url: str, node_id: int) -> str:
        """Construct m3u8 URL from an mp4/mpd URL by reusing the CDN path prefix."""
        match = re.search(
            r"(https?://[^/]+/files/[^/]+/vi/[a-f0-9]{2}/[a-f0-9]{2}/)", video_url
        )
        if match:
            base = match.group(1)
            return f"{base}{node_id}/s{node_id}.m3u8"
        return video_url
