"""网易云音乐 Web API 适配层（零第三方依赖，纯标准库实现）。

使用网易云音乐官方老接口（无需 weapi 签名）：
- 搜索：/api/search/get/web（歌曲 type=1、歌单 type=1000，均无需 Cookie）
- 账号：/api/nuser/account/get（需 Cookie）
- 日推：/api/v1/discovery/recommend/songs（需 Cookie）
- 歌单：/api/user/playlist（需 Cookie）；/api/v1/playlist/detail（无需 Cookie，
  含完整 trackIds；他人歌单内嵌曲目被截断时用 song/detail 分批补全）
- 歌曲详情：/api/song/detail（批量，无需 Cookie，含专辑封面 album.picUrl）
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://music.163.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}

COVER_SIZE = 500  # 封面图展示尺寸（正方形，px）


def enhance_cover_url(url: str, size: int = COVER_SIZE) -> str:
    """归一化网易云图片 URL：先移除旧尺寸参数，再按档位追加清晰度参数。

    - size > 0：强制为 ?param={size}y{size}（同档位保持一致，如 500/1600）
    - size <= 0：原图模式（仅移除 param，返回上传原档，实测最高可达 3000px）
    - http:// 图片自动升级为 https://
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if "?" in url:
        base, query = url.split("?", 1)
        keep = [
            p
            for p in query.split("&")
            if p and not re.fullmatch(r"param=\d+y\d+", p)
        ]
        url = base + (("?" + "&".join(keep)) if keep else "")
    if size and size > 0:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}param={size}y{size}"
    return url


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

    def get_song_cover(self, song_id: int | str, size: int = COVER_SIZE) -> str:
        """获取歌曲封面（专辑封面）URL（无需 Cookie）。

        Returns:
            封面图 URL（默认 500x500 清晰版）；无封面时返回空字符串。
        """
        details = self.get_song_details([int(song_id)])
        if not details:
            return ""
        album = details[0].get("album") or {}
        return enhance_cover_url(album.get("picUrl") or "", size)

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

    def search_playlists(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """搜索歌单（无需 Cookie）。

        Returns:
            歌单 dict 列表，元素含 id/name/trackCount/playCount/creator/coverImgUrl。
        """
        d = self._request(
            f"{API_BASE}/api/search/get/web",
            {"s": keyword, "limit": limit, "type": 1000, "offset": 0},
        )
        return (d.get("result") or {}).get("playlists") or []

    def get_playlist_detail(
        self, playlist_id: int | str, limit: int = 30
    ) -> dict[str, Any] | None:
        """获取歌单详情（无需 Cookie）。

        对他人歌单：详情接口只内嵌前 10~20 首，但 trackIds 完整——按需用
        song/detail 分批（100 首/批）重建完整曲目（实测 668 首约 4.5 秒、
        1214 首约 7 秒，控制 0.12s 批间隔避免风控）。

        Args:
            limit: 期望返回的最大曲目数（默认 30；交互流程传 100000 表示全量）。

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

        tracks = playlist.get("tracks") or []
        track_ids = [
            t.get("id") for t in (playlist.get("trackIds") or []) if t.get("id")
        ]
        # 需要的曲目 id（受 limit 限制）；详情内嵌曲目不足时（他人歌单被截断）分批重建
        need_ids = track_ids[:limit] if limit and limit > 0 else track_ids
        if need_ids and len(tracks) < len(need_ids):
            detail_map: dict[int, dict] = {}
            # song/detail 批量接口一次最多返回约 200 首，按 100 首一批更安全
            for i in range(0, len(need_ids), 100):
                try:
                    batch = self.get_song_details(need_ids[i : i + 100])
                except NCMError:
                    batch = []  # 个别批次失败时跳过，返回其余曲目
                for s in batch:
                    if s.get("id") is not None:
                        detail_map[s["id"]] = s
                time.sleep(0.12)  # 控制请求频率，避免触发风控
            rebuilt = [detail_map[tid] for tid in need_ids if tid in detail_map]
            if rebuilt:
                tracks = rebuilt
        if limit and limit > 0:
            tracks = tracks[:limit]

        # 兜底：内嵌/重建的曲目仍可能缺歌手（老接口精简结构），批量补齐
        missing = [t for t in tracks if t.get("id") and not t.get("artists")]
        if missing:
            detail_map = {}
            ids = [t["id"] for t in missing]
            for i in range(0, len(ids), 100):
                for s in self.get_song_details(ids[i : i + 100]):
                    detail_map[s.get("id")] = s
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
