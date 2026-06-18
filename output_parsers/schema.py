"""output_parsers.schema
Pydantic 数据模型 + Thinking 策略：替代手写 JSON schema 与手写 thinking 字段映射。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ========================================================================
# Column & Row — 数据理解层的结构化输出
# ========================================================================

class ColumnSchema(BaseModel):
    """单个列的 schema 描述。"""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="列名")
    type: str = Field(..., description="列类型：number / string / datetime / boolean")
    role: Optional[str] = Field(default=None, description="列语义角色：dimension / measure / x-axis / y-axis / other")
    description: Optional[str] = Field(default=None, description="可选的人类可读描述")
    unit: Optional[str] = Field(default=None, description="单位，如 '元' / '%'")


class DataUnderstandingResponse(BaseModel):
    """LLM 整理数据后的结构化输出（用 :meth:`ChatOpenAIWrapper.structured` 调用 LLM）。"""

    model_config = ConfigDict(extra="ignore")

    columns: List[ColumnSchema] = Field(..., description="整理后的列元信息")
    rows: List[Dict[str, Any]] = Field(..., description="整理后的行数据")
    summary: Optional[str] = Field(default=None, description="数据概览描述")
    notes: Optional[str] = Field(default=None, description="整理过程中的备注与异常说明")


# ========================================================================
# Chart 生成层 — 结构化输出
# ========================================================================

class ChartGenerationResponse(BaseModel):
    """LLM 生成图表后的结构化输出（双模式支持）。

    通过 :meth:`ChatOpenAIWrapper.structured` 调 LLM 直接返回 Pydantic 对象。

    支持两种模式（``option`` 和 ``code`` 至少一个非空）：

    - **option 模式**（旧）：``option`` 是严格 JSON 配置 dict，formatter 字段必须用字符串模板
    - **code 模式**（新，推荐）：``code`` 是完整可执行 JS 代码段，含 ``const option = {...}`` 定义
      与 ``chart.setOption(option)`` 调用；option 内可使用任意 ECharts 回调函数
      （formatter / label.formatter / axisLabel.formatter 等）

    校验策略：

    - option 模式：Pydantic 强类型校验（``option: Dict[str, Any]``）
    - code 模式：仅校验非空字符串（具体 JS 合法性由前端 sandbox iframe 验证）
    """

    model_config = ConfigDict(extra="ignore")

    option: Optional[Dict[str, Any]] = Field(
        default=None, description="mode=option 时的 JSON 配置对象（不可包含函数）"
    )
    code: Optional[str] = Field(
        default=None, description="mode=code 时的完整 JS 代码段（含 chart.setOption(option) 调用）"
    )
    content: str = Field(..., description="图表的文字解读（中文）")

    @model_validator(mode="after")
    def _check_at_least_one(self) -> "ChartGenerationResponse":
        if not self.option and not self.code:
            raise ValueError("Either 'option' (JSON) or 'code' (JS) must be provided")
        return self

    @property
    def is_code_mode(self) -> bool:
        return self.code is not None


# ========================================================================
# Chart 类型推荐
# ========================================================================

class ChartTypeRecommendation(BaseModel):
    """图表类型推荐的结构化输出。"""

    chart_type: str = Field(..., description="ECharts 系列名，如 bar / line / pie / scatter / radar / gauge / funnel / heatmap / sunburst / treemap / sankey / candlestick / boxplot / effectScatter / pictorialBar")
    reason: str = Field(..., description="中文说明推荐理由")


# ========================================================================
# Thinking 策略 — 多态化取代 ProviderConfig 里的 thinking_* 字段
# ========================================================================

class ThinkingStrategy(ABC):
    """把 ``llm_thinking`` 的用户取值（``"" / off / low / medium / high``）翻译成 LLM extra kwargs 的策略。

    各 provider 的字段名（``reasoning_effort`` vs ``thinking``）、off 时是"不发送"还是
    发特定值（Ollama 需 ``"none"``、GLM 需 ``{type: disabled}``）、值映射关系各不相同，
    拆成多态类让每个 provider 自己负责。
    """

    @abstractmethod
    def resolve(self, user_value: str) -> Dict[str, Any]:
        """``user_value``: ``"" / "off" / "low" / "medium" / "high"``（大小写不限）。"""


class NoThinkingStrategy(ThinkingStrategy):
    """Provider 不支持 thinking 控制：始终返回空 dict。"""

    def resolve(self, user_value: str) -> Dict[str, Any]:
        return {}


class OpenAIStyleStrategy(ThinkingStrategy):
    """OpenAI / DeepSeek / DashScope：``reasoning_effort``，``off`` 时不发送任何字段。"""

    _EFFORTS: ClassVar[Dict[str, str]] = {"low": "low", "medium": "medium", "high": "high"}

    def resolve(self, user_value: str) -> Dict[str, Any]:
        v = (user_value or "").strip().lower()
        if v in ("", "off"):
            return {}
        return {"reasoning_effort": self._EFFORTS.get(v, v)}


class OllamaStyleStrategy(ThinkingStrategy):
    """Ollama：``reasoning_effort``，``off`` 必须显式发 ``"none"`` 才会真正关闭。"""

    _EFFORTS: ClassVar[Dict[str, str]] = {"low": "low", "medium": "medium", "high": "high"}

    def resolve(self, user_value: str) -> Dict[str, Any]:
        v = (user_value or "").strip().lower()
        if v in ("", "off"):
            return {"reasoning_effort": "none"}
        return {"reasoning_effort": self._EFFORTS.get(v, v)}


class GLMStyleStrategy(ThinkingStrategy):
    """智谱 GLM-4.5+：``thinking`` 字段；``off`` 发 ``{type: disabled}``，其它都发 ``{type: enabled}``。"""

    def resolve(self, user_value: str) -> Dict[str, Any]:
        v = (user_value or "").strip().lower()
        if v in ("", "off"):
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}}


# ========================================================================
# Provider 配置 — 用 ThinkingStrategy 多态取代之前的 thinking_* 字段组
# ========================================================================

class ProviderConfig(BaseModel):
    """Provider 的运行时配置：name + 该 provider 专属的 thinking 策略。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(..., description="provider 标识：openai / ollama / glm")
    thinking: ThinkingStrategy = Field(default_factory=NoThinkingStrategy, description="该 provider 的 thinking 控制策略")

    # ---------- factory ----------

    @classmethod
    def for_provider(cls, name: str) -> "ProviderConfig":
        """工厂：根据 provider 名称返回预置策略。

        扩展新 provider 时只需在此加一个 ``elif`` 分支。
        """
        n = (name or "").lower()
        if n == "ollama":
            return cls(name="ollama", thinking=OllamaStyleStrategy())
        if n == "glm":
            return cls(name="glm", thinking=GLMStyleStrategy())
        # 默认 openai 兼容（OpenAI / DeepSeek / DashScope 等）
        return cls(name="openai", thinking=OpenAIStyleStrategy())

    def resolve_extra(self, user_value: str) -> Dict[str, Any]:
        """把用户输入的 ``llm_thinking`` 值解析为传给 LLM 的 extra kwargs。"""
        return self.thinking.resolve(user_value)


# ========================================================================
# Pipeline 事件协议（参考性 schema；运行时由 app.py 用裸 dict 产出）
# ========================================================================

class PipelineEvent(BaseModel):
    """Pipeline 的统一事件对象（参考性 schema）。运行时由 :func:`app.run_chart_pipeline` 直接 yield 裸 dict；此处仅作为协议文档化。

    event 字典结构::

        {"type": "stage",   "stage": "<name>", "status": "start|done|skipped|error", ...}
        {"type": "delta",   "content": "<token chunk>"}
        {"type": "done",    "chart_type": "...", "option": {...}, "content": "...", ...}
        {"type": "error",   "message": "...", "status": 502, "raw_reply": "..."}
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(..., description="事件类型：stage / delta / done / error")
    stage: Optional[str] = Field(default=None, description="阶段名：prepare / understand / preprocess / pick_type / generate / parse")
    status: Optional[str] = Field(default=None, description="阶段状态：start / done / skipped / error")
    content: Optional[str] = Field(default=None, description="delta 事件的 token 增量，或 error 事件的消息")
    data: Optional[Dict[str, Any]] = Field(default=None, description="阶段附属数据（可选）")
