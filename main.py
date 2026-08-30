"""HDS Interlude 的 AstrBot 插件入口。

把 AstrBot 消息事件桥接到 HDSI 核心叙事，并把模型调用接到 AstrBot Provider。
"""

from __future__ import annotations

from typing import Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .adapters.astrbot_bridge import AstrbotBridge
from .core.model import ChatMessage, ModelConfig


class HDSInterludePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.bridge = AstrbotBridge(self._data_dir(), self.config)

    def _data_dir(self) -> str:
        """插件专属数据目录：data/plugin_data/astrbot_plugin_hds_interlude/"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = get_astrbot_data_path() / "plugin_data" / self.name
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"hds-interlude: cannot resolve data dir, fallback to ./data: {err}")
            import os

            path = os.path.join("data", "plugin_data", self.name)
            os.makedirs(path, exist_ok=True)
            return path

    # ---- LLM Provider 桥接 ----
    def _build_astrobot_call(self, event: AstrMessageEvent):
        async def call(messages: list[ChatMessage], cfg: ModelConfig) -> str:
            try:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception as err:  # noqa: BLE001
                logger.warn(f"hds-interlude: get provider failed: {err}")
                provider_id = None
            if not provider_id:
                raise RuntimeError("未获取到 AstrBot 会话 provider id")

            # system 与 user 合并为 AstrBot 的 prompt/system_prompt
            system_prompt = ""
            prompt_parts: list[str] = []
            for m in messages:
                if m.role == "system":
                    system_prompt = m.content
                else:
                    prompt_parts.append(f"{m.role}: {m.content}")
            prompt = "\n\n".join(prompt_parts)

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            return getattr(llm_resp, "completion_text", "") or ""
        return call

    # ---- 普通私聊文本（OneBot/NapCat）----
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_private_message(self, event: AstrMessageEvent):
        """捕获 OneBot/NapCat 私聊文本，进入 HDSI 叙事。"""
        text = (event.message_str or "").strip()
        if not text:
            return
        # 管理命令跳过：hdsi.* 前缀
        if text.startswith("hdsi.") or text.startswith("interlude."):
            return

        # 可选白名单：只在配置了 allowed_user_ids 且非空时启用。
        allowed = self.config.get("runtime", {}).get("allowed_user_ids") or []
        user_id = str(event.get_sender_id())
        if allowed and user_id not in allowed:
            logger.info(f"hds-interlude: 拒绝未授权用户 {user_id}")
            return

        platform = event.get_platform_name()
        self_id = str(event.self_id)
        channel_id = str(event.session_id)

        reply, decision, consumed = await self.bridge.handle_user_message(
            platform, self_id, user_id, channel_id, text,
            get_provider_callable=self._build_astrobot_call(event),
        )
        if not consumed:
            return
        if reply:
            yield event.plain_result(reply)
        else:
            # 沉默决策：吞掉消息，不让它落入其他处理器
            yield event.plain_result("")

    # ---- 管理命令 ----
    @filter.command("hdsi")
    async def hdsi(self, event: AstrMessageEvent):
        """HDS Interlude 管理命令入口。"""
        yield event.plain_result("HDS Interlude 已加载。子命令：/hdsi.status")

    @filter.command("hdsi.status")
    async def hdsi_status(self, event: AstrMessageEvent):
        """查看当前主剧本状态。"""
        story = self.bridge._story
        if not story:
            yield event.plain_result("当前没有活动主剧本。")
            return
        c = story.setting.character_name
        updates = story.state.narrative_update_count
        entries = len(self.bridge._entries)
        facts = len(self.bridge._facts)
        yield event.plain_result(
            f"主剧本：{c}\n叙事轮次：{updates}\n剧本条目：{entries}\n长期事实：{facts}"
        )

    async def terminate(self):
        """插件卸载时清理。"""
        if self.bridge:
            self.bridge._persist()
