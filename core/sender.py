"""音乐卡片发送器。

使用 OneBot v11 的 music 消息类型（type=163，网易云），由 NapCat
渲染成可点击播放的 QQ 音乐卡片。仅支持 aiocqhttp（NapCat/OneBot v11）
平台；其他平台返回 False，由调用方降级为文本链接。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

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
