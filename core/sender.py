"""消息发送器：音乐卡片、图片消息。

- 音乐卡片：使用 OneBot v11 的 music 消息类型（type=163，网易云），由 NapCat
  渲染成可点击播放的 QQ 音乐卡片。仅支持 aiocqhttp（NapCat/OneBot v11）
  平台；其他平台返回 False，由调用方降级为文本链接。
- 图片消息：发送网络图片（如歌曲/歌单封面）。aiocqhttp 平台直接传递图片 URL，
  其他平台尝试通用图片消息链。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

try:  # AstrBot v4 内部 API
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # pragma: no cover - 平台不可用时降级
    AiocqhttpMessageEvent = None  # type: ignore[assignment,misc]


class MusicCardSender:
    @staticmethod
    def song_link(song_id: int | str) -> str:
        return f"https://music.163.com/song?id={song_id}"

    @staticmethod
    async def send_music_card(event: AstrMessageEvent, song_id: int | str) -> bool:
        """发送网易云音乐卡片。

        Returns:
            True 表示已成功发送卡片；False 表示平台不支持或发送失败。
        """
        if AiocqhttpMessageEvent is None or not isinstance(
            event, AiocqhttpMessageEvent
        ):
            return False

        payloads: dict = {
            "message": [{"type": "music", "data": {"type": "163", "id": int(song_id)}}]
        }
        try:
            if event.is_private_chat():
                payloads["user_id"] = event.get_sender_id()
                await event.bot.api.call_action("send_private_msg", **payloads)
            else:
                payloads["group_id"] = event.get_group_id()
                await event.bot.api.call_action("send_group_msg", **payloads)
            return True
        except Exception as e:
            logger.error(f"音乐卡片发送失败: {e}")
            return False

    @staticmethod
    async def send_image(event: AstrMessageEvent, url: str) -> bool:
        """发送网络图片（歌曲/歌单封面等）。

        aiocqhttp 平台优先走原始图片段（直接把 URL 交给 NapCat 拉取）；
        其他平台尝试通用图片消息链。

        Returns:
            True 表示已成功发送；False 表示平台不支持或发送失败。
        """
        url = (url or "").strip()
        if not url:
            return False

        if AiocqhttpMessageEvent is not None and isinstance(
            event, AiocqhttpMessageEvent
        ):
            payloads: dict = {
                "message": [{"type": "image", "data": {"file": url}}]
            }
            try:
                if event.is_private_chat():
                    payloads["user_id"] = event.get_sender_id()
                    await event.bot.api.call_action("send_private_msg", **payloads)
                else:
                    payloads["group_id"] = event.get_group_id()
                    await event.bot.api.call_action("send_group_msg", **payloads)
                return True
            except Exception as e:
                logger.error(f"图片发送失败: {e}")
                return False

        # 其他平台：通用图片消息链
        try:
            await event.send(MessageChain([Image.fromURL(url)]))
            return True
        except Exception as e:
            logger.warning(f"图片发送失败（通用链路）: {e}")
            return False

    @staticmethod
    async def recall_message(event: AstrMessageEvent, message_id: object) -> bool:
        """撤回一条由本插件发送的消息（点歌/选择列表页）。

        仅 aiocqhttp（NapCat/OneBot v11）平台支持；非该平台、消息不存在、
        超时或无权限等情况一律静默失败并返回 False，不影响主流程。

        Returns:
            True 表示撤回成功或无需撤回（message_id 为空也视为成功）。
        """
        if message_id is None:
            return True
        if AiocqhttpMessageEvent is None or not isinstance(
            event, AiocqhttpMessageEvent
        ):
            return False
        try:
            await event.bot.api.call_action("delete_msg", message_id=int(message_id))
            return True
        except Exception as e:
            logger.warning(f"撤回消息失败（忽略）: {e}")
            return False
