"""astrbot_plugin_ncm_daily - 网易云音乐助手插件。

功能：
- 搜索音乐（无需 Cookie）
- 发送网易云音乐卡片（QQ 群/私聊，NapCat 渲染）
- 每日推荐（需 MUSIC_U Cookie）
- 个人歌单（需 MUSIC_U Cookie）
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.ncm import NCMError, NetEaseMusic
from .core.sender import MusicCardSender

COOKIE_TIP = (
    "未配置 MUSIC_U Cookie。请在插件配置中填入网易云 MUSIC_U Cookie"
    "（浏览器登录 music.163.com 后按 F12 → 网络 → 复制任意请求 Cookie 里的 MUSIC_U 值）。"
)


class NcmDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cookie = str(config.get("music_u_cookie", "") or "").strip()
        self.ncm = NetEaseMusic(cookie)
        self.sender = MusicCardSender()

    # ---------- 工具 ----------

    @filter.llm_tool()
    async def search_music(
        self, event: AstrMessageEvent, keyword: str, limit: int = 10
    ):
        """搜索网易云音乐歌曲，返回歌曲列表（序号、歌名、歌手、专辑、时长）。

        Args:
            keyword(string): 搜索关键词，歌名或歌手
            limit(int): 返回数量，默认 10，最大 20
        """
        limit = max(1, min(int(limit), 20))
        try:
            songs = self.ncm.search_songs(keyword, limit)
        except NCMError as e:
            return f"搜索失败：{e}"
        if not songs:
            return f"没有找到与「{keyword}」相关的歌曲"
        return self._format_songs(songs)

    @filter.llm_tool()
    async def get_daily_recommend(self, event: AstrMessageEvent, count: int = 10):
        """获取网易云今日每日推荐歌曲（需配置 MUSIC_U Cookie）。

        Args:
            count(int): 返回数量，默认 10，最大 20
        """
        if not self.ncm.logged_in:
            return COOKIE_TIP
        count = max(1, min(int(count), 20))
        try:
            songs = self.ncm.get_daily_recommend()
        except NCMError as e:
            return f"获取每日推荐失败：{e}"
        if not songs:
            return "今日日推为空，可能是 Cookie 已失效或今日暂无推荐"
        return self._format_songs(songs[:count])

    @filter.llm_tool()
    async def get_my_playlists(self, event: AstrMessageEvent):
        """获取当前网易云账号（MUSIC_U）的歌单列表（歌单名、歌曲数、播放量）。

        Returns:
            歌单列表文本；未配置 Cookie 时返回配置提示。
        """
        if not self.ncm.logged_in:
            return COOKIE_TIP
        try:
            playlists = self.ncm.get_user_playlists(limit=30)
        except NCMError as e:
            return f"获取歌单失败：{e}"
        if not playlists:
            return "没有获取到歌单，可能是 Cookie 已失效"
        lines = []
        for i, pl in enumerate(playlists, 1):
            name = pl.get("name", "")
            tracks = pl.get("trackCount", 0)
            plays = pl.get("playCount", 0)
            lines.append(f"{i}. {name}（{tracks} 首，播放 {plays}）id={pl.get('id')}")
        return "\n".join(lines)

    @filter.llm_tool()
    async def get_playlist_detail(self, event: AstrMessageEvent, playlist_id: int):
        """查看指定网易云歌单的歌曲列表（歌单 ID 来自 get_my_playlists 或用户提供）。

        Args:
            playlist_id(int): 网易云歌单 ID
        """
        try:
            playlist = self.ncm.get_playlist_detail(playlist_id, limit=30)
        except NCMError as e:
            return f"获取歌单详情失败：{e}"
        if not playlist:
            return f"没有找到歌单 id={playlist_id}"
        name = playlist.get("name", "")
        tracks = playlist.get("tracks") or []
        if not tracks:
            return f"歌单「{name}」暂时没有可展示的歌曲"
        lines = [f"歌单「{name}」共 {playlist.get('trackCount', '?')} 首，展示前 {len(tracks)} 首："]
        for i, s in enumerate(tracks, 1):
            lines.append(self._format_song(i, s))
        return "\n".join(lines)

    @filter.llm_tool()
    async def play_music(
        self, event: AstrMessageEvent, song_id: int, song_name: str = ""
    ):
        """向当前会话发送网易云音乐卡片（QQ 群内可直接点击播放）。

        Args:
            song_id(int): 网易云歌曲 ID（来自搜索/日推/歌单结果）
            song_name(string): 歌曲名称，用于发送失败时的提示（可选）
        """
        name = song_name or f"id={song_id}"
        ok = await self.sender.send_music_card(event, song_id)
        if ok:
            return f"已发送音乐卡片《{name}》"
        link = self.sender.song_link(song_id)
        return f"当前平台不支持音乐卡片，可点击链接试听：{link}"

    # ---------- 格式化 ----------

    @staticmethod
    def _format_song(index: int, s: dict) -> str:
        name = s.get("name", "")
        artists = "、".join(
            a.get("name", "") for a in (s.get("artists") or [])
        )
        album = (s.get("album") or {}).get("name", "")
        duration = s.get("duration") or 0
        minutes, seconds = divmod(duration // 1000, 60)
        return f"{index}. {name} - {artists} | {album} | {minutes}:{seconds:02d} | id={s.get('id')}"

    def _format_songs(self, songs: list[dict]) -> str:
        return "\n".join(self._format_song(i, s) for i, s in enumerate(songs, 1))