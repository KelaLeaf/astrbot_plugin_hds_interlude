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
        self._apply_persona_if_set()

    def _apply_persona_if_set(self) -> None:
        """配置页选了 persona_id（在 story 组）时，启动即把该 AstrBot 人格导入为角色设定。"""
        persona_id = None
        try:
            if isinstance(self.config, dict):
                # persona_id 现位于 story 子组；兼容旧顶层读取
                story = self.config.get("story", {})
                if isinstance(story, dict):
                    persona_id = story.get("persona_id") or self.config.get("persona_id")
                else:
                    persona_id = self.config.get("persona_id")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"hds-interlude: read persona_id failed: {err}")
        if not persona_id:
            return
        try:
            pm = self.context.persona_manager
            persona = pm.get_persona(persona_id)
            system_prompt = getattr(persona, "system_prompt", "") or ""
            info = self.bridge.import_persona(persona_id=str(persona_id), system_prompt=system_prompt)
            logger.info(f"hds-interlude: 已从配置导入人格「{persona_id}」→ 角色「{info['character_name']}」")
        except Exception as err:  # noqa: BLE001
            logger.warn(f"hds-interlude: apply persona '{persona_id}' failed: {err}")

    def _data_dir(self) -> str:
        """插件专属数据目录：data/plugin_data/astrbot_plugin_hds_interlude/"""
        plugin_name = "astrbot_plugin_hds_interlude"
        try:
            from pathlib import Path

            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            # get_astrbot_data_path() 返回 str，需转 Path 才能用 / 拼接
            path = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            path.mkdir(parents=True, exist_ok=True)
            return str(path)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"hds-interlude: cannot resolve data dir, fallback to ./data: {err}")
            import os

            path = os.path.join("data", "plugin_data", plugin_name)
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

            # 可选：让叙事模型感知 AstrBot 会话工具（含 MCP/知识库/网页搜索）。
            # 默认关闭，避免破坏结构化 JSON 叙事。开启后模型可声明调用外部工具。
            tools = None
            if self.config.get("runtime", {}).get("narrative_tools", False):
                tools = self._get_conversation_tools(umo)
            kwargs = {}
            if tools is not None:
                kwargs["tools"] = tools

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                **kwargs,
            )
            return getattr(llm_resp, "completion_text", "") or ""
        return call

    def _get_conversation_tools(self, umo: str | None = None):
        """尝试获取 AstrBot 会话可用工具集；API 不可用则返回 None（不崩溃）。"""
        try:
            runner = self.context.get_using_agent_runner(umo=umo) if umo else self.context.get_using_agent_runner()
            if runner is not None and hasattr(runner, "tool_manager"):
                return getattr(runner, "tool_manager")
            return None
        except Exception as err:  # noqa: BLE001
            logger.warn(f"hds-interlude: get conversation tools failed: {err}")
            return None

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

    @filter.command("hdsi.import_persona <persona_id>")
    async def hdsi_import_persona(self, event: AstrMessageEvent, persona_id: str):
        """把一个 AstrBot 人格一键导入为本插件的角色设定。"""
        try:
            pm = self.context.persona_manager
            persona = pm.get_persona(persona_id)
        except Exception as err:  # noqa: BLE001
            yield event.plain_result(f"读取人格失败：{err}")
            return

        system_prompt = getattr(persona, "system_prompt", "") or ""
        info = self.bridge.import_persona(
            persona_id=persona_id,
            system_prompt=system_prompt,
        )
        yield event.plain_result(
            f"已导入人格「{persona_id}」：角色名「{info['character_name']}」，"
            f"设定 {info['profile_length']} 字。重启/触发叙事后生效。"
        )

    @filter.command("hdsi.personas")
    async def hdsi_personas(self, event: AstrMessageEvent):
        """列出 AstrBot 可导入的人格。"""
        try:
            pm = self.context.persona_manager
            personas = pm.get_all_personas()
        except Exception as err:  # noqa: BLE001
            yield event.plain_result(f"读取人格列表失败：{err}")
            return
        lines = [f"共 {len(personas)} 个人格："]
        for p in personas:
            pid = getattr(p, "persona_id", None) or getattr(p, "id", "")
            name = getattr(p, "name", "") or getattr(p, "id", "")
            lines.append(f"- {pid}（{name}）")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时清理。"""
        if self.bridge:
            self.bridge._persist()
