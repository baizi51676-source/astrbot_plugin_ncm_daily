"""astrbot_plugin_ncm_daily - 网易云音乐助手插件。

功能：
- 搜索音乐（无需 Cookie）
- 发送网易云音乐卡片（QQ 群/私聊，NapCat 渲染）
- 每日推荐（需 MUSIC_U Cookie，仅管理员）
- 个人歌单（需 MUSIC_U Cookie，仅管理员）
- 点歌指令（白名单用户）
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.ncm import NCMError, NetEaseMusic
from .core.sender import MusicCardSender

COOKIE_TIP = (
    "未配置 MUSIC_U Cookie。请在插件配置中填入网易云 MUSIC_U Cookie"
    "（浏览器登录 music.163.com 后按 F12 → 网络 → 复制任意请求 Cookie 里的 MUSIC_U 值）。"
)

PAGE_SIZE = 200  # 歌单歌曲列表每页数量
MSG_LIMIT = 4000  # 单条消息安全长度（字符），超出则截断并提示翻页
POINT_CMD = "点歌"  # 点歌指令前缀
POINT_LIMIT = 10  # 点歌搜索结果数量
DAILY_CMDS = ("日推", "今日推荐")  # 日推指令
WAIT_TIMEOUT = 120  # 歌单/日推交互等待超时（秒）
POINT_TIMEOUT = 30  # 点歌交互等待超时（秒，默认）


class NcmDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cookie = str(config.get("music_u_cookie", "") or "").strip()
        self.ncm = NetEaseMusic(cookie)
        self.sender = MusicCardSender()
        self._waiting: dict[str, dict] = {}
        """等待交互状态：key = "{origin}:{sender}"，仅发起者本人可操作。
        字段：playlists / tracks / offset / expiry
        """
        # 仅管理员开关（默认开启）：关闭后我的歌单/歌单详情/日推对所有人开放
        self.admin_only = bool(config.get("admin_only", True))
        # 点歌白名单 QQ 列表；为空 = 不限制；管理员始终可点歌
        raw_allow = config.get("point_song_allowlist", []) or []
        self.point_allowlist = {
            str(x).strip() for x in raw_allow if str(x).strip()
        }
        # 点歌交互超时（秒，默认 30）
        try:
            self.point_timeout = max(5, int(config.get("point_timeout", POINT_TIMEOUT)))
        except (TypeError, ValueError):
            self.point_timeout = POINT_TIMEOUT

    # ---------- 等待状态与超时 ----------

    def _start_timeout_task(
        self, key: str, state: dict, event: AstrMessageEvent
    ) -> None:
        """注册等待状态后启动超时主动提示任务：到期自动发消息提醒用户。"""
        try:
            asyncio.create_task(self._timeout_worker(key, state, event))
        except RuntimeError:
            pass  # 事件循环不可用时（极少数场景）退化为仅清理

    async def _timeout_worker(
        self, key: str, state: dict, event: AstrMessageEvent
    ) -> None:
        """超时任务：state 的 expiry 更新后自动适应，用户已操作则静默退出。"""
        try:
            while True:
                remaining = state["expiry"] - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 1))
            # 状态仍是同一个（未被用户操作清理）才提示
            if self._waiting.get(key) is state:
                self._waiting.pop(key, None)
                mode = state.get("mode", "")
                tip = {
                    "point": "点歌超时",
                    "daily": "日推选择超时",
                    "playlist": "选择超时",
                }.get(mode, "选择超时")
                await event.send(event.plain_result(f"{tip}，已退出。可重新发起。"))
        except Exception as e:
            logger.warning(f"[ncm] 超时任务异常: {e}")

    # ---------- 权限 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员：使用 AstrBot 自带机制（不额外维护列表）。

        优先级：
        1. event.is_admin()（discord/kook 等平台有效）
        2. OneBot 群角色 owner/admin（群主/群管理员，从原始事件取）
        3. AstrBot 全局配置 admins_id（WebUI 配置页面设置）
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            raw = getattr(event, "message_obj", None)
            raw = getattr(raw, "raw_message", None)
            if raw is not None and hasattr(raw, "get"):
                role = (raw.get("sender") or {}).get("role", "")
                if role in ("owner", "admin"):
                    return True
        except Exception:
            pass
        try:
            conf = self.context.get_conf(event.unified_msg_origin)
            admins = (conf or {}).get("admins_id", []) or []
            return str(event.get_sender_id()) in {str(a).strip() for a in admins}
        except Exception:
            try:
                conf = self.context.get_conf(None)
                admins = (conf or {}).get("admins_id", []) or []
                return str(event.get_sender_id()) in {str(a).strip() for a in admins}
            except Exception:
                pass
        return False

    def _admin_tip(self) -> str:
        """非管理员时的提示文案。"""
        return (
            "该功能仅管理员可用。请在 AstrBot 配置页面（WebUI → 配置 → "
            "admins_id 管理员列表）中添加你的 QQ 后重试。"
        )

    def _can_point_song(self, event: AstrMessageEvent) -> bool:
        """是否允许点歌：管理员或白名单；白名单为空则不限制。"""
        if self._is_admin(event):
            return True
        if not self.point_allowlist:
            return True
        return str(event.get_sender_id()) in self.point_allowlist

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
        if self.admin_only and not self._is_admin(event):
            return self._admin_tip()
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
        if self.admin_only and not self._is_admin(event):
            return self._admin_tip()
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
        if self.admin_only and not self._is_admin(event):
            return self._admin_tip()
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

    # ---------- 命令交互（我的歌单，自实现状态机） ----------

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"

    @filter.command("我的歌单", alias={"歌单", "查看歌单"})
    async def my_playlists_cmd(self, event: AstrMessageEvent):
        """我的歌单、歌单、查看歌单：列出歌单，回复序号选择，再回复序号或歌名播放（仅本人可操作）"""
        pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_my_playlists(self, event: AstrMessageEvent):
        """命令入口 + 交互输入处理（不依赖 AstrBot session_waiter）"""
        key = self._session_key(event)
        state = self._waiting.get(key)
        logger.debug(f"[ncm] key={key} state={'有' if state else '无'} text={event.message_str.strip()!r}")

        # ---- 交互输入：等待状态中的消息直接处理并拦截 ----
        if state is not None:
            event.stop_event()
            if time.time() > state["expiry"]:
                self._waiting.pop(key, None)
                await event.send(event.plain_result("选择超时，已退出"))
                return
            await self._handle_input(event, key, state)
            return

        # ---- 命令入口 ----
        if not event.is_at_or_wake_command:
            return
        text = event.message_str.strip()

        # 点歌指令（白名单）：点歌 [歌手 - ]歌名
        if text.startswith(POINT_CMD):
            await self._point_song(event, key, text[len(POINT_CMD):].strip())
            return

        # 日推指令（仅管理员，admin_only 可关）：日推 / 今日推荐
        if text in DAILY_CMDS:
            await self._daily_recommend(event, key)
            return

        if text not in ("我的歌单", "歌单", "查看歌单"):
            return
        event.stop_event()

        # 仅管理员可用（默认开启，可配置 admin_only 关闭）
        if self.admin_only and not self._is_admin(event):
            await event.send(event.plain_result(self._admin_tip()))
            return

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

        # 先注册等待状态，再发送列表（不依赖 yield 之后代码执行）
        state = {
            "playlists": playlists,
            "tracks": [],
            "offset": 0,
            "expiry": time.time() + WAIT_TIMEOUT,
            "mode": "playlist",
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        items = [f"{i}. {pl.get('name')}（{pl.get('trackCount')} 首）" for i, pl in enumerate(playlists, 1)]
        await self._send_text_list(
            event,
            "🎵 你的歌单（回复序号选择，仅你本人可操作）：",
            items,
        )

    async def _point_song(self, event: AstrMessageEvent, key: str, query: str) -> None:
        """点歌指令：搜索歌曲并列出（回复序号播放）。"""
        if not query:
            await event.send(event.plain_result("用法：点歌 歌手 - 歌名 或 点歌 歌名"))
            return
        if not self._can_point_song(event):
            await event.send(
                event.plain_result("你没有点歌权限（需加入插件配置 point_song_allowlist 白名单）。")
            )
            return
        event.stop_event()

        # 解析歌手与歌名：优先 "歌手 - 歌名"（支持 - 无空格）
        artist, name = "", query
        for sep in (" - ", "-"):
            if sep in query:
                parts = query.split(sep, 1)
                artist, name = parts[0].strip(), parts[1].strip()
                break
        keyword = f"{artist} {name}".strip() if artist else name
        if not keyword:
            await event.send(event.plain_result("歌名不能为空"))
            return

        try:
            songs = self.ncm.search_songs(keyword, POINT_LIMIT)
        except NCMError as e:
            await event.send(event.plain_result(f"搜索失败：{e}"))
            return
        if not songs:
            await event.send(event.plain_result(f"没有找到「{keyword}」相关的歌曲"))
            return

        # 注册等待状态（点歌模式：playlists 为空，tracks=搜索结果）
        state = {
            "playlists": [],
            "tracks": songs,
            "offset": 0,
            "expiry": time.time() + self.point_timeout,
            "mode": "point",
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 点歌已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        items = [self._format_song(i, s) for i, s in enumerate(songs, 1)]
        await self._send_text_list(
            event,
            f"🎵 「{keyword}」的搜索结果：",
            items,
            hint="回复序号播放，或直接回复歌名重新搜索",
        )

    async def _daily_recommend(
        self, event: AstrMessageEvent, key: str
    ) -> None:
        """日推指令：列出今日推荐（合并消息卡片），回复序号播放。"""
        event.stop_event()
        if self.admin_only and not self._is_admin(event):
            await event.send(event.plain_result(self._admin_tip()))
            return
        if not self.ncm.logged_in:
            await event.send(event.plain_result(COOKIE_TIP))
            return
        try:
            songs = self.ncm.get_daily_recommend()
        except NCMError as e:
            await event.send(event.plain_result(f"获取每日推荐失败：{e}"))
            return
        if not songs:
            await event.send(event.plain_result("今日日推为空，可能是 Cookie 已失效"))
            return

        # 注册等待状态（日推模式：playlists 为空，tracks=日推列表）
        state = {
            "playlists": [],
            "tracks": songs,
            "offset": 0,
            "expiry": time.time() + WAIT_TIMEOUT,
            "mode": "daily",
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 日推已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        items = [self._format_song(i, s) for i, s in enumerate(songs, 1)]
        await self._send_text_list(
            event,
            "🎵 今日推荐（回复序号播放，仅你本人可操作）：",
            items,
            hint="回复序号播放，或直接回复歌名搜索",
        )

    async def _handle_input(self, event: AstrMessageEvent, key: str, state: dict) -> None:
        """处理等待中的用户输入：选歌单 / 选歌 / 翻页 / 歌名搜索。"""
        text = event.message_str.strip()

        if not state["tracks"]:
            # ---- 阶段1：选择歌单 ----
            if not text.isdigit():
                return
            idx = int(text)
            if idx < 1 or idx > len(state["playlists"]):
                await event.send(
                    event.plain_result(
                        f"序号超出范围（1-{len(state['playlists'])}），请重新输入"
                    )
                )
                return
            pl = state["playlists"][idx - 1]
            try:
                detail = self.ncm.get_playlist_detail(pl.get("id"), limit=100000)
            except NCMError as e:
                await event.send(event.plain_result(f"获取歌单失败：{e}"))
                self._waiting.pop(key, None)
                return
            if not detail:
                await event.send(event.plain_result("歌单不存在"))
                self._waiting.pop(key, None)
                return
            state["tracks"] = detail.get("tracks") or []
            state["offset"] = 0
            state["expiry"] = time.time() + WAIT_TIMEOUT
            await self._send_song_list(event, detail, state["tracks"], 0)
            return

        # ---- 阶段2：选择歌曲 ----
        low = text.lower()
        if low in ("更多", "下一页", "下页"):
            if state["offset"] + PAGE_SIZE >= len(state["tracks"]):
                await event.send(event.plain_result("已经是最后一页了"))
            else:
                state["offset"] += PAGE_SIZE
                state["expiry"] = time.time() + WAIT_TIMEOUT
                await self._send_song_list(
                    event, None, state["tracks"], state["offset"]
                )
            return

        if text.isdigit():
            idx = int(text)
            if idx < 1 or idx > len(state["tracks"]):
                await event.send(
                    event.plain_result(
                        f"序号超出范围（1-{len(state['tracks'])}），请重新输入"
                    )
                )
                return
            await self._send_and_stop(event, key, state["tracks"][idx - 1])
            return

        # 按歌名搜索并播放
        try:
            songs = self.ncm.search_songs(text, 1)
        except NCMError as e:
            await event.send(event.plain_result(f"搜索失败：{e}"))
            return
        if not songs:
            await event.send(event.plain_result(f"没有找到「{text}」相关的歌曲"))
            return
        await self._send_and_stop(event, key, songs[0])

    # ---------- 交互辅助 ----------

    async def _send_text_list(
        self,
        event: AstrMessageEvent,
        title: str,
        items: list[str],
        hint: str = "",
    ) -> None:
        """以合并转发（聊天记录卡片）形式发送列表：卡片内仅一条消息（完整多行文本）。

        聊天界面只显示一个卡片，不占屏、不被 QQ 折叠拆分；点开后就是完整的
        多行文本（类似直接回复文本的效果）。超过 MSG_LIMIT 字符时截断并提示翻页。
        发送失败（非 aiocqhttp 平台等）自动降级为普通文本消息。
        """
        text = title + "\n" + "\n".join(items)
        truncated = False
        if len(text) > MSG_LIMIT:
            parts = [title]
            cur = len(title)
            shown = 0
            for line in items:
                if cur + len(line) + 1 > MSG_LIMIT:
                    truncated = True
                    break
                parts.append(line)
                cur += len(line) + 1
                shown += 1
            text = "\n".join(parts)
            if truncated:
                hint = (
                    (hint + "；" if hint else "")
                    + f"列表较长，已展示前 {shown} 项，输入「更多」查看后续"
                )
        if hint:
            text += "\n" + hint

        try:
            self_id = str(getattr(event, "get_self_id", lambda: "0")() or "0")
        except Exception:
            self_id = "0"
        # 合并转发卡片：仅一条 node，内容为完整多行文本
        try:
            node = Node(
                content=[Plain(text)],
                name="网易云音乐助手",
                uin=self_id,
            )
            await event.send(MessageChain([Nodes([node])]))
            return
        except Exception as e:
            logger.warning(f"[ncm] 合并转发发送失败，降级为普通消息: {e}")
        await event.send(event.plain_result(text))

    async def _send_song_list(
        self,
        event: AstrMessageEvent,
        detail: dict | None,
        tracks: list[dict],
        offset: int,
    ) -> None:
        """把歌单歌曲列表作为一条普通文本消息发出（从 1 开始编号，每页最多 PAGE_SIZE 首）。"""
        start = offset + 1
        end = min(offset + PAGE_SIZE, len(tracks))
        if end < start:
            end = start
        if detail:
            total = detail.get("trackCount", "?")
            title = f"🎵 歌单「{detail.get('name')}」共 {total} 首，展示第 {start}-{end} 首："
        else:
            title = f"🎵 继续展示第 {start}-{end} 首："
        items = []
        for i, s in enumerate(tracks[offset:end], start):
            artists = "、".join(
                a.get("name", "") for a in (s.get("artists") or [])
            )
            items.append(f"{i}. {s.get('name')} - {artists}")
        await self._send_text_list(
            event,
            title,
            items,
            hint="回复序号播放，或直接回复歌名搜索；输入「更多」查看下一页",
        )

    async def _send_and_stop(
        self,
        event: AstrMessageEvent,
        key: str,
        song: dict,
    ) -> None:
        """发送歌曲（卡片或链接）并结束交互会话。"""
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
        self._waiting.pop(key, None)

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