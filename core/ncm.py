"""网易云音乐 Web API 适配层（零第三方依赖，纯标准库实现）。

使用网易云音乐官方老接口（无需 weapi 签名）：
- 搜索：/api/search/get/web（无需 Cookie）
- 账号：/api/nuser/account/get（需 Cookie）
- 日推：/api/v1/discovery/recommend/songs（需 Cookie）
- 歌单：/api/user/playlist、/api/v1/playlist/detail（歌单详情无需 Cookie）
- 歌曲详情：/api/song/detail（批量，无需 Cookie）
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "http://music.163.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}


class NCMError(Exception):
    """网易云 API 调用异常。"""


class NetEaseMusic:
    """网易云音乐客户端。

    Args:
        music_u_cookie: 网易云 MUSIC_U Cookie 值（不带前缀）。
            仅获取每日推荐/个人歌单时需要；搜索无需登录。
    """

    def __init__(self, music_u_cookie: str = "") -> None:
        value = (music_u_cookie or "").strip().strip(";")
        parts = ["appver=2.0.2"]
        if value:
            parts.insert(0, f"MUSIC_U={value}")
        self.cookie = "; ".join(parts)
        self._uid: int | None = None

    @property
    def logged_in(self) -> bool:
        return self.cookie.startswith("MUSIC_U=")

    # ---------- 底层请求 ----------

    def _request(self, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(
            url, data=body, headers={**HEADERS, "Cookie": self.cookie}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise NCMError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise NCMError(str(e)) from e

    # ---------- 账号 ----------

    def get_account(self) -> dict[str, Any] | None:
        """获取当前账号信息（需 Cookie）。未登录或失效时返回 None。"""
        d = self._request(f"{API_BASE}/api/nuser/account/get", {})
        return d.get("profile")

    def _get_uid(self) -> int | None:
        if self._uid:
            return self._uid
        profile = self.get_account()
        if profile:
            self._uid = profile.get("userId")
        return self._uid

    # ---------- 歌曲 ----------

    def search_songs(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """搜索歌曲（无需 Cookie）。

        Returns:
            歌曲 dict 列表，元素含 id/name/artists/album/duration。
        """
        d = self._request(
            f"{API_BASE}/api/search/get/web",
            {"s": keyword, "limit": limit, "type": 1, "offset": 0},
        )
        return (d.get("result") or {}).get("songs") or []

    def get_song_details(self, song_ids: list[int]) -> list[dict[str, Any]]:
        """批量获取歌曲详情（无需 Cookie）。"""
        if not song_ids:
            return []
        d = self._request(
            f"{API_BASE}/api/song/detail", {"ids": json.dumps(song_ids, separators=(",", ":"))}
        )
        return d.get("songs") or []

    # ---------- 歌单 ----------

    def get_user_playlists(self, limit: int = 30) -> list[dict[str, Any]]:
        """获取当前账号创建/收藏的歌单（需 Cookie）。

        Returns:
            歌单 dict 列表，元素含 id/name/trackCount/playCount。
        """
        uid = self._get_uid()
        if not uid:
            raise NCMError("无法获取账号信息，请检查 MUSIC_U Cookie 是否有效或已过期")
        d = self._request(
            f"{API_BASE}/api/user/playlist",
            {"uid": uid, "limit": limit, "offset": 0},
        )
        return d.get("playlist") or []

    def get_playlist_detail(
        self, playlist_id: int | str, limit: int = 30
    ) -> dict[str, Any] | None:
        """获取歌单详情（无需 Cookie）。

        Returns:
            歌单 dict（含 name/trackCount/tracks）或 None。
        """
        d = self._request(
            f"{API_BASE}/api/v1/playlist/detail",
            {"id": playlist_id, "n": 100000},  # n 控制返回曲目数，不传则默认仅 10 首
        )
        playlist = d.get("playlist")
        if not playlist:
            return None
        tracks = (playlist.get("tracks") or [])[:limit]
        # 老接口返回的 track 可能是精简结构（只有 id/name），
        # 若缺少歌手/专辑/时长信息则批量补齐
        if tracks and not tracks[0].get("artists"):
            detail_map = {
                s.get("id"): s
                for s in self.get_song_details([t.get("id") for t in tracks if t.get("id")])
            }
            tracks = [detail_map.get(t.get("id"), t) for t in tracks]
        playlist["tracks"] = tracks
        return playlist

    # ---------- 每日推荐 ----------

    def get_daily_recommend(self) -> list[dict[str, Any]]:
        """获取每日推荐歌曲（需 Cookie）。

        Returns:
            歌曲 dict 列表（含 id/name/artists/album）。
        """
        d = self._request(f"{API_BASE}/api/v1/discovery/recommend/songs", {})
        recommend = d.get("recommend") or []
        songs: list[dict[str, Any]] = []
        pending_ids: list[int] = []
        for item in recommend:
            if isinstance(item, dict) and item.get("id"):
                songs.append(item)
            elif isinstance(item, int):
                pending_ids.append(item)
        if pending_ids:
            songs.extend(self.get_song_details(pending_ids))
        return songs
