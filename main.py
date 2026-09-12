"""astrbot_plugin_ncm_daily - 网易云音乐助手插件。

功能：
- 搜索音乐（无需 Cookie）
- 发送网易云音乐卡片（QQ 群/私聊，NapCat 渲染）
- 歌曲封面（无需 Cookie）
- 每日推荐（需 MUSIC_U Cookie，仅管理员；菜单头部含第一首歌封面）
- 个人歌单（需 MUSIC_U Cookie，仅管理员；选歌列表头部含歌单封面，支持多选与超长歌单分条展示）
- 点歌指令（白名单用户）
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.ncm import NCMError, NetEaseMusic, enhance_cover_url
from .core.sender import MusicCardSender
try:  # AstrBot v4 内部 API：aiocqhttp（NapCat/OneBot v11）平台事件
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # pragma: no cover - 平台不可用时降级
    AiocqhttpMessageEvent = None  # type: ignore[assignment,misc]


COOKIE_TIP = (
    "未配置 MUSIC_U Cookie。请在插件配置中填入网易云 MUSIC_U Cookie"
    "（浏览器登录 music.163.com 后按 F12 → 网络 → 复制任意请求 Cookie 里的 MUSIC_U 值）。"
)

SONGS_PER_MSG = 100  # 合并转发卡片内每条消息最多展示的歌曲数（超出自动分多条）
MULTI_PLAY_LIMIT = 20  # 一次回复多个序号时最多播放的歌曲数
MSG_LIMIT = 4000  # 单条消息安全长度（字符），超出则截断并提示翻页
POINT_CMD = "点歌"  # 点歌指令前缀
POINT_LIMIT = 10  # 点歌搜索结果数量
DAILY_CMDS = ("日推", "今日推荐")  # 日推指令
COVER_CMDS = ("歌曲封面", "封面")  # 歌曲封面指令前缀（长的在前）
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
        字段：playlists / tracks / offset / expiry / menu_msg_ids（本次交互已发菜单卡片的消息 id 列表）
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
                # 顺带撤回等待期间残留的全部菜单卡片（仅 aiocqhttp 平台有效，失败静默）
                await self._recall_menus(event, state)
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
        # 白名单控制（管理员/白名单用户；白名单为空=不限制），防止任意调用触发风控
        if not self._can_point_song(event):
            return "你没有使用音乐功能的权限（需加入插件配置 point_song_allowlist 白名单）。"
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

        成功时不返回提示文本（卡片本身即反馈）；失败时返回可试听链接。

        Args:
            song_id(int): 网易云歌曲 ID（来自搜索/日推/歌单结果）
            song_name(string): 歌曲名称（保留兼容，不再用于提示）
        """
        # 白名单控制（管理员/白名单用户；白名单为空=不限制）
        if not self._can_point_song(event):
            return "你没有使用音乐功能的权限（需加入插件配置 point_song_allowlist 白名单）。"
        # 成功不返回提示文本：卡片本身即反馈，避免“已发送《…》音乐卡片”冗余播报
        ok = await self.sender.send_music_card(event, song_id)
        if ok:
            return ""
        link = self.sender.song_link(song_id)
        return f"当前平台不支持音乐卡片，可点击链接试听：{link}"

    @filter.llm_tool()
    async def get_song_cover(self, event: AstrMessageEvent, keyword: str):
        """发送指定歌曲的封面图片到当前会话（QQ 群内直接显示图片）。

        成功时不返回提示文本（图片本身即反馈）；失败时返回原因或可打开的链接。

        Args:
            keyword(string): 歌名，或「歌手 - 歌名」（更精确）
        """
        # 白名单控制（管理员/白名单用户；白名单为空=不限制）
        if not self._can_point_song(event):
            return "你没有使用音乐功能的权限（需加入插件配置 point_song_allowlist 白名单）。"
        return await self._fetch_and_send_cover(event, (keyword or "").strip())

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
            # 空文本事件（图片/表情/撤回通知等）直接忽略：不响应、不拦截
            if not event.message_str.strip():
                return
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

        # 封面指令（白名单）：封面 / 歌曲封面 + [歌手 - ]歌名
        for prefix in COVER_CMDS:
            if text.startswith(prefix):
                await self._cover_song(event, text[len(prefix):].strip())
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
            "menu_msg_ids": [],
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        items = [f"{i}. {pl.get('name')}（{pl.get('trackCount')} 首）" for i, pl in enumerate(playlists, 1)]
        mid = await self._send_text_list(
            event,
            "你的歌单（回复序号选择，仅你本人可操作）：",
            items,
        )
        if mid is not None:
            state["menu_msg_ids"].append(mid)

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
            "menu_msg_ids": [],
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 点歌已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        items = [self._format_song(i, s) for i, s in enumerate(songs, 1)]
        mid = await self._send_text_list(
            event,
            f"「{keyword}」的搜索结果：",
            items,
            hint="回复序号播放，或直接回复歌名重新搜索",
        )
        if mid is not None:
            state["menu_msg_ids"].append(mid)

    async def _cover_song(self, event: AstrMessageEvent, query: str) -> None:
        """封面指令：搜索歌曲并发送其封面图片。"""
        if not query:
            await event.send(
                event.plain_result("用法：封面 歌手 - 歌名 或 封面 歌名")
            )
            return
        if not self._can_point_song(event):
            await event.send(
                event.plain_result("你没有使用音乐功能的权限（需加入插件配置 point_song_allowlist 白名单）。")
            )
            return
        event.stop_event()
        tip = await self._fetch_and_send_cover(event, query)
        if tip:
            await event.send(event.plain_result(tip))

    async def _fetch_and_send_cover(self, event: AstrMessageEvent, query: str) -> str:
        """搜索并发送歌曲封面，返回面向用户的提示文本（成功时为空字符串）。

        支持「歌手 - 歌名」格式；权限校验由调用方负责。
        """
        artist, name = "", query
        for sep in (" - ", "-"):
            if sep in query:
                parts = query.split(sep, 1)
                artist, name = parts[0].strip(), parts[1].strip()
                break
        keyword = f"{artist} {name}".strip() if artist else name
        if not keyword:
            return "歌名不能为空"
        try:
            songs = self.ncm.search_songs(keyword, 1)
        except NCMError as e:
            return f"搜索失败：{e}"
        if not songs:
            return f"没有找到「{keyword}」相关的歌曲"
        song = songs[0]
        try:
            cover = self.ncm.get_song_cover(song.get("id"))
        except NCMError as e:
            return f"获取封面失败：{e}"
        if not cover:
            return f"没有找到《{song.get('name')}》的封面图"
        ok = await self.sender.send_image(event, cover)
        if ok:
            return ""
        return f"封面发送失败，可打开链接查看：{cover}"

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
            "menu_msg_ids": [],
        }
        self._waiting[key] = state
        logger.debug(f"[ncm] 日推已注册等待状态: {key}")
        self._start_timeout_task(key, state, event)

        # 菜单头部图：使用第一首歌的封面（获取失败则不带图）
        cover = ""
        first = songs[0] if songs else None
        if first and first.get("id"):
            try:
                cover = self.ncm.get_song_cover(first.get("id"))
            except NCMError:
                cover = ""

        items = [self._format_song(i, s) for i, s in enumerate(songs, 1)]
        mid = await self._send_text_list(
            event,
            "今日推荐（回复序号播放，仅你本人可操作）：",
            items,
            hint="回复序号播放，或直接回复歌名搜索",
            image=cover,
        )
        if mid is not None:
            state["menu_msg_ids"].append(mid)

    async def _handle_input(self, event: AstrMessageEvent, key: str, state: dict) -> None:
        """处理等待中的用户输入：选歌单 / 选歌 / 翻页 / 歌名搜索。"""
        text = event.message_str.strip()
        # 空文本防御：不触发任何操作（图片/表情/撤回通知等）
        if not text:
            return

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
            await self._send_song_list(event, detail, state["tracks"], 0, state)
            return

        # ---- 阶段2：选择歌曲 ----
        low = text.lower()
        if low in ("更多", "下一页", "下页"):
            await event.send(
                event.plain_result("歌曲已一次性完整展示，请直接回复序号（支持多个，如 1 7 98）")
            )
            return

        # 多选：一次回复多个序号（空格 / 逗号 / 顿号分隔，如 "1 7 98"）
        tokens = [
            t
            for t in text.replace(",", " ").replace("，", " ").replace("、", " ").split()
            if t
        ]
        if len(tokens) >= 2 and all(t.isdigit() for t in tokens):
            await self._play_multiple(event, key, state, [int(t) for t in tokens])
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
        image: str = "",
        per_msg: int = 0,
    ) -> int | None:
        """以合并转发（聊天记录卡片）形式发送列表。

        聊天界面只显示一个卡片，不占屏、不被 QQ 折叠拆分；点开后是完整内容。
        - 默认（per_msg=0）：卡片内仅一条消息；超过 MSG_LIMIT 字符时截断并提示翻页；
        - 传入 per_msg（如 100）：每 per_msg 项拆成一条消息，同卡片内依次展示，
          不做截断（用于超长歌单：268 首 → 3 条消息）。
        发送失败（非 aiocqhttp 平台等）自动降级为普通文本消息。

        Args:
            image: 可选图片 URL，作为卡片头部内容先于文本展示（如歌单/歌曲封面）。
            per_msg: 0 或负数时单条展示；正数时按该数量分条（每条最多 per_msg 项）。

        Returns:
            aiocqhttp 平台下返回该列表消息的 message_id（供交互完成后撤回）；
            其他平台或降级发送时返回 None。
        """
        texts: list[str] = []
        if per_msg and per_msg > 0 and items:
            chunks = [items[i : i + per_msg] for i in range(0, len(items), per_msg)]
            for ci, chunk in enumerate(chunks):
                parts: list[str] = []
                if ci == 0:
                    parts.append(title)
                parts.extend(chunk)
                if ci == len(chunks) - 1 and hint:
                    parts.append(hint)
                texts.append("\n".join(parts))
        else:
            text = title + "\n" + "\n".join(items)
            if len(text) > MSG_LIMIT:
                parts = [title]
                cur = len(title)
                shown = 0
                for line in items:
                    if cur + len(line) + 1 > MSG_LIMIT:
                        break
                    parts.append(line)
                    cur += len(line) + 1
                    shown += 1
                text = "\n".join(parts)
                if shown < len(items):
                    hint = (
                        (hint + "；" if hint else "")
                        + f"列表较长，已展示前 {shown} 项，输入「更多」查看后续"
                    )
            if hint:
                text += "\n" + hint
            texts.append(text)
        try:
            self_id = str(getattr(event, "get_self_id", lambda: "0")() or "0")
        except Exception:
            self_id = "0"

        # aiocqhttp（NapCat/OneBot v11）：直接以 node（转发）形式走底层 API 发送，
        # 换取 message_id，供选歌完成/交互超时后撤回该列表消息（与 core/sender.py 同一套调用）。
        if AiocqhttpMessageEvent is not None and isinstance(
            event, AiocqhttpMessageEvent
        ):
            message: list = []
            for i, text in enumerate(texts):
                node_content: list = []
                if i == 0 and image:
                    node_content.append({"type": "image", "data": {"file": image}})
                node_content.append({"type": "text", "data": {"text": text}})
                message.append(
                    {
                        "type": "node",
                        "data": {
                            "uin": self_id,
                            "name": "网易云音乐助手",
                            "content": node_content,
                        },
                    }
                )
            payloads: dict = {"message": message}
            try:
                if event.is_private_chat():
                    payloads["user_id"] = event.get_sender_id()
                    result = await event.bot.api.call_action(
                        "send_private_msg", **payloads
                    )
                else:
                    payloads["group_id"] = event.get_group_id()
                    result = await event.bot.api.call_action(
                        "send_group_msg", **payloads
                    )
                mid = (result or {}).get("message_id")
                return int(mid) if mid is not None else None
            except Exception as e:
                logger.warning(f"[ncm] 合并转发发送失败，降级为普通消息: {e}")

        # 降级发送：普通合并转发卡片；仍失败则再降级为纯文本
        try:
            nodes: list = []
            for i, text in enumerate(texts):
                components: list = []
                if i == 0 and image:
                    try:
                        components.append(Image.fromURL(image))
                    except Exception:
                        pass  # 图片组件不可用时退化为纯文本列表
                components.append(Plain(text))
                nodes.append(
                    Node(content=components, name="网易云音乐助手", uin=self_id)
                )
            await event.send(MessageChain([Nodes(nodes)]))
        except Exception as e:
            logger.warning(f"[ncm] 合并转发发送失败，降级为普通消息: {e}")
            await event.send(event.plain_result("\n".join(texts)))
        return None

    async def _send_song_list(
        self,
        event: AstrMessageEvent,
        detail: dict | None,
        tracks: list[dict],
        offset: int,
        state: dict | None = None,
    ) -> int | None:
        """把歌单歌曲列表作为合并转发卡片发出（从 1 开始编号，整单一次性展示）。

        每个消息最多展示 SONGS_PER_MSG 首，超出自动分为多条消息（如 268 首 → 3 条）；
        歌单封面展示在卡片头部。

        Args:
            offset: 保留兼容位（当前不再分页）。
            state: 交互等待状态；传入时把卡片 message_id 追加到 state["menu_msg_ids"]，
                歌单封面缓存到 state["cover"]。

        Returns:
            同 _send_text_list：aiocqhttp 平台返回 message_id，否则 None。
        """
        total_view = len(tracks)
        if detail:
            total = detail.get("trackCount", total_view)
            title = f"歌单「{detail.get('name')}」共 {total} 首："
        else:
            title = f"共 {total_view} 首："
        cover = enhance_cover_url((detail or {}).get("coverImgUrl") or "")
        if not cover and state is not None:
            cover = state.get("cover", "")
        if state is not None and cover:
            state["cover"] = cover
        items = []
        for i, s in enumerate(tracks, 1):
            artists = "、".join(
                a.get("name", "") for a in (s.get("artists") or [])
            )
            items.append(f"{i}. {s.get('name')} - {artists}")
        mid = await self._send_text_list(
            event,
            title,
            items,
            hint="回复序号播放（支持一次回复多个序号，用空格分隔，如 1 7 98）",
            image=cover,
            per_msg=SONGS_PER_MSG,
        )
        if state is not None and mid is not None:
            state.setdefault("menu_msg_ids", []).append(mid)
        return mid

    async def _send_and_stop(
        self,
        event: AstrMessageEvent,
        key: str,
        song: dict,
    ) -> None:
        """发送歌曲（卡片或链接）并结束交互会话。

        播放成功后不追加“已发送《…》音乐卡片”之类提示（卡片本身即反馈），
        并撤回本次交互的全部菜单卡片（歌单选择/歌曲列表等，仅 aiocqhttp 平台有效，失败静默）。
        """
        ok = await self.sender.send_music_card(event, song.get("id"))
        state = self._waiting.get(key)
        if ok:
            if state is not None:
                await self._recall_menus(event, state)
        else:
            await event.send(
                event.plain_result(
                    f"当前平台不支持音乐卡片，可点击试听：{self.sender.song_link(song.get('id'))}"
                )
            )
        self._waiting.pop(key, None)

    async def _play_multiple(
        self,
        event: AstrMessageEvent,
        key: str,
        state: dict,
        indices: list[int],
    ) -> None:
        """多选播放：一次回复多个序号，逐首发送音乐卡片并结束交互。

        - 自动去重（保持输入顺序）；
        - 单次最多播放 MULTI_PLAY_LIMIT 首；
        - 超范围的序号将被跳过并提示。
        """
        seen: set[int] = set()
        unique: list[int] = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                unique.append(idx)

        valid: list[int] = []
        invalid: list[int] = []
        for idx in unique:
            if 1 <= idx <= len(state["tracks"]):
                valid.append(idx)
            else:
                invalid.append(idx)
        overflow = len(valid) > MULTI_PLAY_LIMIT
        if overflow:
            valid = valid[:MULTI_PLAY_LIMIT]

        for idx in valid:
            song = state["tracks"][idx - 1]
            ok = await self.sender.send_music_card(event, song.get("id"))
            if not ok:
                await event.send(
                    event.plain_result(
                        f"《{song.get('name')}》当前平台不支持音乐卡片，"
                        f"可点击试听：{self.sender.song_link(song.get('id'))}"
                    )
                )

        await self._recall_menus(event, state)
        self._waiting.pop(key, None)

        notes: list[str] = []
        if overflow:
            notes.append(f"一次最多播放 {MULTI_PLAY_LIMIT} 首，其余已忽略")
        if invalid:
            notes.append("超出范围的序号已跳过：" + "、".join(str(x) for x in invalid))
        if notes:
            await event.send(event.plain_result("；".join(notes)))

    async def _recall_menus(self, event: AstrMessageEvent, state: dict) -> None:
        """撤回本次交互已发送的全部菜单卡片（仅 aiocqhttp 平台有效，失败静默）。"""
        for mid in state.get("menu_msg_ids") or []:
            await self.sender.recall_message(event, mid)

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