import time
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.star.star_tools import StarTools


PLUGIN_NAME = "astrbot_plugin_redline_alert"


def _normalize_id_list(values) -> list[str]:
    """将配置项统一转成字符串 ID 列表。"""
    if not values:
        return []

    if isinstance(values, str):
        values = [values]

    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.append(text)
    return result


def _truncate(text: str, max_len: int) -> str:
    """按最大长度截断文本。"""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


@dataclass
class RedlineAlertTool(FunctionTool[AstrAgentContext]):
    name: str = "redline_alert"
    description: str = (
        "工具中文名为“红线预警”。当当前对话涉及明显高风险内容时调用，尤其是政治红线、煽动颠覆、分裂国家、"
        "极端敏感现实政治、组织非法行动、严重违规引导等内容。"
        "调用后会自动把当前对话的概括、危险点、风险等级发送给管理员在插件配置中预设的群聊或用户，"
        "并用于提醒系统该话题存在风险。"
        "仅在你已经明确判断该对话具有较高风险时调用，不要用于普通争议、学术讨论或安全无害的话题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "risk_level": {
                    "type": "string",
                    "description": "风险等级，例如 low、medium、high、critical。建议高风险时填写 high 或 critical。",
                },
                "danger_points": {
                    "type": "string",
                    "description": "危险点总结，说明具体哪里危险，尽量简洁明确。",
                },
                "conversation_summary": {
                    "type": "string",
                    "description": "对当前对话的总结，便于管理员快速理解上下文。",
                },
                "suggested_response": {
                    "type": "string",
                    "description": "建议采取的处理方式，例如拒绝继续该话题、引导更换话题等。",
                },
            },
            "required": ["risk_level", "danger_points", "conversation_summary"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        ctx = context.context.context
        event = context.context.event

        stars = ctx.get_all_stars()
        plugin = None
        for star in stars:
            if star.star_cls and isinstance(star.star_cls, RedlineAlertPlugin):
                plugin = star.star_cls
                break

        if plugin is None:
            return "红线预警插件未初始化，无法发送预警。"

        if not plugin.enabled:
            return "红线预警插件当前未启用。"

        summary = _truncate(kwargs.get("conversation_summary", ""), plugin.max_summary_length)
        danger_points = _truncate(kwargs.get("danger_points", ""), plugin.max_danger_length)
        risk_level = str(kwargs.get("risk_level", "high")).strip() or "high"
        suggested_response = _truncate(
            kwargs.get("suggested_response", ""),
            plugin.max_suggestion_length,
        )

        if not summary or not danger_points:
            return "红线预警调用失败：conversation_summary 或 danger_points 为空。"

        platform_id = event.get_platform_id() or plugin.platform
        session_key = f"{platform_id}:{event.unified_msg_origin}"

        if plugin.cooldown_seconds > 0:
            now = time.time()
            last_time = plugin._last_alert_ts.get(session_key, 0.0)
            if now - last_time < plugin.cooldown_seconds:
                remain = int(plugin.cooldown_seconds - (now - last_time))
                return (
                    f"该会话已在冷却时间内发送过红线预警，无需重复发送。"
                    f"剩余冷却约 {max(remain, 0)} 秒。"
                )

        message_text = plugin.build_alert_message(
            platform_id=platform_id,
            sender_id=event.get_sender_id(),
            group_id=event.get_group_id(),
            risk_level=risk_level,
            danger_points=danger_points,
            conversation_summary=summary,
            suggested_response=suggested_response,
        )
        message_chain = MessageChain().message(message_text)

        sent_targets: list[str] = []
        failed_targets: list[str] = []

        for user_id in plugin.notify_user_ids:
            try:
                await StarTools.send_message_by_id(
                    "PrivateMessage",
                    user_id,
                    message_chain,
                    platform=plugin.platform,
                )
                sent_targets.append(f"用户:{user_id}")
            except Exception as e:
                failed_targets.append(f"用户:{user_id}({e})")
                logger.warning(f"[{PLUGIN_NAME}] 向用户 {user_id} 发送红线预警失败: {e}")

        for group_id in plugin.notify_group_ids:
            try:
                await StarTools.send_message_by_id(
                    "GroupMessage",
                    group_id,
                    message_chain,
                    platform=plugin.platform,
                )
                sent_targets.append(f"群:{group_id}")
            except Exception as e:
                failed_targets.append(f"群:{group_id}({e})")
                logger.warning(f"[{PLUGIN_NAME}] 向群 {group_id} 发送红线预警失败: {e}")

        if sent_targets:
            plugin._last_alert_ts[session_key] = time.time()

        if not sent_targets and failed_targets:
            return (
                "已识别到高风险对话，但预警发送失败。"
                f"失败目标：{'；'.join(failed_targets)}。"
                "请提示管理员检查插件配置、平台类型和目标 ID。"
            )

        if not sent_targets:
            return "已识别到高风险对话，但未配置任何预警接收目标。请管理员在插件设置中填写群号或用户号。"

        result_lines = [
            "已触发红线预警并完成上报。",
            f"风险等级：{risk_level}",
            f"已发送到：{'，'.join(sent_targets)}",
            "请停止继续该高风险话题，改为礼貌拒绝，并提醒对方更换安全话题。",
        ]
        if failed_targets:
            result_lines.append(f"部分发送失败：{'；'.join(failed_targets)}")
        return "\n".join(result_lines)


@register(PLUGIN_NAME, "ciyua", "当 LLM 检测到政治红线等高风险对话时自动发送预警", "1.0.0")
class RedlineAlertPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.enabled = config.get("enabled", True)
        self.platform = str(config.get("platform", "aiocqhttp")).strip() or "aiocqhttp"
        self.notify_user_ids = _normalize_id_list(config.get("notify_user_ids", []))
        self.notify_group_ids = _normalize_id_list(config.get("notify_group_ids", []))
        self.cooldown_seconds = max(int(config.get("cooldown_seconds", 300) or 0), 0)
        self.max_summary_length = max(int(config.get("max_summary_length", 300) or 300), 50)
        self.max_danger_length = max(int(config.get("max_danger_length", 200) or 200), 30)
        self.max_suggestion_length = max(
            int(config.get("max_suggestion_length", 120) or 120),
            20,
        )
        self.alert_title = str(config.get("alert_title", "【红线预警】")).strip() or "【红线预警】"
        self.include_origin = bool(config.get("include_origin", True))

        self._last_alert_ts: dict[str, float] = {}

        self.context.add_llm_tools(RedlineAlertTool())

    def build_alert_message(
        self,
        platform_id: str,
        sender_id: str,
        group_id: str,
        risk_level: str,
        danger_points: str,
        conversation_summary: str,
        suggested_response: str,
    ) -> str:
        """构造要发送给管理员的预警消息。"""
        lines = [self.alert_title]
        lines.append(f"平台：{platform_id}")
        if self.include_origin:
            lines.append(f"发送者 ID：{sender_id or '未知'}")
            if group_id:
                lines.append(f"群聊 ID：{group_id}")
            else:
                lines.append("会话类型：私聊")
        lines.append(f"风险等级：{risk_level}")
        lines.append(f"危险点：{danger_points}")
        lines.append(f"对话总结：{conversation_summary}")
        if suggested_response:
            lines.append(f"建议处理：{suggested_response}")
        return "\n".join(lines)
