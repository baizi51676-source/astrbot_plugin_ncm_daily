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
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .core.ncm import NCMError, NetEaseMusic
from .core.sender import MusicCardSender

COOKIE_TIP = (
    "未配置 MUSIC_U Cookie。请在插件配置中填入网易云 MUSIC_U Cookie"
    "（浏览器登录 music.163.com 后按 F12 → 网络 → 复制任意请求 Cookie 里的 MUSIC_U 值）。"
)

PAGE_SIZE = 30  # 歌单歌曲列表每页数量


class _UserSessionFilter(SessionFilter):
    """会话隔离：群/会话 + 发送者。

    只有发起命令的本人回复才会进入选择流程，同群其他人回复无效。
    """

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


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

    # ---------- 命令交互（我的歌单） ----------

    @filter.command("我的歌单", alias={"歌单", "查看歌单"})
    async def my_playlists_cmd(self, event: AstrMessageEvent):
        """我的歌单、歌单、查看歌单：列出歌单，回复序号选择，再回复序号或歌名播放（仅本人可操作）"""
        pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_my_playlists(self, event: AstrMessageEvent):
        """命令入口：我的歌单 / 歌单 / 查看歌单"""
        if not event.is_at_or_wake_command:
            return
        text = event.message_str.strip()
        if text not in ("我的歌单", "歌单", "查看歌单"):
            return
        event.stop_event()

        if not self.ncm.logged_in:
            yield event.plain_result(COOKIE_TIP)
            return
        try:
            playlists = self.ncm.get_user_playlists(limit=30)
        except NCMError as e:
            yield event.plain_result(f"获取歌单失败：{e}")
            return
        if not playlists:
            yield event.plain_result("没有获取到歌单，可能是 Cookie 已失效")
            return

        lines = ["🎵 你的歌单（回复序号选择，仅你本人可操作）："]
        for i, pl in enumerate(playlists, 1):
            lines.append(f"{i}. {pl.get('name')}（{pl.get('trackCount')} 首）")
        yield event.plain_result("\n".join(lines))

        # 交互状态：selected_tracks 为空 = 等待选歌单；否则 = 等待选歌
        selected_tracks: list[dict] = []
        offset = 0

        @session_waiter(timeout=120)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            nonlocal selected_tracks, offset
            text = ev.message_str.strip()

            if not selected_tracks:
                # ---- 阶段1：选择歌单 ----
                if not text.isdigit():
                    return
                idx = int(text)
                if idx < 1 or idx > len(playlists):
                    await ev.send(
                        ev.plain_result(f"序号超出范围（1-{len(playlists)}），请重新输入")
                    )
                    return
                pl = playlists[idx - 1]
                try:
                    detail = self.ncm.get_playlist_detail(pl.get("id"), limit=100000)
                except NCMError as e:
                    await ev.send(ev.plain_result(f"获取歌单失败：{e}"))
                    controller.stop()
                    return
                if not detail:
                    await ev.send(ev.plain_result("歌单不存在"))
                    controller.stop()
                    return
                selected_tracks = detail.get("tracks") or []
                offset = 0
                await self._send_song_list(ev, detail, selected_tracks, offset)
                controller.keep(120, reset_timeout=True)
                return

            # ---- 阶段2：选择歌曲 ----
            low = text.lower()
            if low in ("更多", "下一页", "下页"):
                if offset + PAGE_SIZE >= len(selected_tracks):
                    await ev.send(ev.plain_result("已经是最后一页了"))
                else:
                    offset += PAGE_SIZE
                    await self._send_song_list(ev, None, selected_tracks, offset)
                controller.keep(120, reset_timeout=True)
                return

            if text.isdigit():
                idx = int(text)
                if idx < 1 or idx > len(selected_tracks):
                    await ev.send(
                        ev.plain_result(
                            f"序号超出范围（1-{len(selected_tracks)}），请重新输入"
                        )
                    )
                    return
                await self._send_and_stop(controller, ev, selected_tracks[idx - 1])
                return

            # 按歌名搜索并播放
            try:
                songs = self.ncm.search_songs(text, 1)
            except NCMError as e:
                await ev.send(ev.plain_result(f"搜索失败：{e}"))
                return
            if not songs:
                await ev.send(ev.plain_result(f"没有找到「{text}」相关的歌曲"))
                return
            await self._send_and_stop(controller, ev, songs[0])

        try:
            await waiter(event, session_filter=_UserSessionFilter())
        except TimeoutError:
            yield event.plain_result("选择超时，已退出")

    # ---------- 交互辅助 ----------

    async def _send_song_list(
        self,
        event: AstrMessageEvent,
        detail: dict | None,
        tracks: list[dict],
        offset: int,
    ) -> None:
        """把歌单歌曲列表作为一条消息发出（从 1 开始编号）。"""
        start = offset + 1
        end = min(offset + PAGE_SIZE, len(tracks))
        if end < start:
            end = start
        lines = []
        if detail:
            total = detail.get("trackCount", "?")
            lines.append(
                f"🎵 歌单「{detail.get('name')}」共 {total} 首，展示第 {start}-{end} 首："
            )
        else:
            lines.append(f"🎵 继续展示第 {start}-{end} 首：")
        for i, s in enumerate(tracks[offset:end], start):
            artists = "、".join(
                a.get("name", "") for a in (s.get("artists") or [])
            )
            lines.append(f"{i}. {s.get('name')} - {artists}")
        lines.append("回复序号播放，或直接回复歌名搜索；输入「更多」查看下一页")
        await event.send(event.plain_result("\n".join(lines)))

    async def _send_and_stop(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        song: dict,
    ) -> None:
        """发送歌曲（卡片或链接）并结束会话。"""
        name = song.get("name", "未知歌曲")
        ok = await self.sender.send_music_card(event, song.get("id"))
        if ok:
            await event.send(event.plain_result(f"已发送《{name}》音乐卡片"))
        else:
            await event.send(
                event.plain_result(
                    f"当前平台不支持音乐卡片，可点击试听：{self.sender.song_link(song.get('id'))}"
                )
            )
        controller.stop()

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