"""output_parsers.schema
Pydantic 数据模型：替代手写 JSON schema，提供编译期/运行期类型检查。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

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
    """LLM 整理数据后的结构化输出。

    与 data_understanding.py 的原始 JSON schema 保持字段对齐；
    同时给出 Pydantic 级别的默认值与校验。
    """

    model_config = ConfigDict(extra="ignore")

    columns: List[ColumnSchema] = Field(..., description="整理后的列元信息")
    rows: List[Dict[str, Any]] = Field(..., description="整理后的行数据")
    summary: Optional[str] = Field(default=None, description="数据概览描述")
    notes: Optional[str] = Field(default=None, description="整理过程中的备注与异常说明")

    # ---------- helpers ----------

    def to_flat_columns(self) -> List[str]:
        """与旧接口兼容：返回纯列名列表。"""
        return [c.name for c in self.columns]


# ========================================================================
# Chart 生成层 — 结构化输出
# ========================================================================

class ChartGenerationResponse(BaseModel):
    """LLM 生成图表后的结构化输出。

    替代手写 `_CHART_RESPONSE_SCHEMA` dict；
    通过 ChatOpenAI.with_structured_output() 让 LLM 直接返回 Pydantic 对象。
    """

    model_config = ConfigDict(extra="ignore")

    option: Dict[str, Any] = Field(..., description="ECharts 配置对象")
    content: str = Field(..., description="图表的文字解读（中文）")

    # ---------- helpers ----------

    def to_code(self) -> str:
        """生成可直接用于 HTML 输出的代码文本。"""
        import json
        return f"""const option = {json.dumps(self.option, ensure_ascii=False, indent=2)};"""

    def to_explanation(self) -> str:
        """向后兼容：旧前端字段名为 explanation。"""
        return self.content


# ========================================================================
# Chart 类型推荐
# ========================================================================

class ChartTypeRecommendation(BaseModel):
    """图表类型推荐的结构化输出。"""

    chart_type: str = Field(..., description="ECharts 系列名，如 bar / line / pie / scatter / radar / gauge / funnel / heatmap / sunburst / treemap / sankey / candlestick / boxplot / effectScatter / pictorialBar")
    reason: str = Field(..., description="中文说明推荐理由")


# ========================================================================
# Provider 配置 — 用于替代手写的 _detect_provider / _resolve_thinking_field
# ========================================================================

class ProviderConfig(BaseModel):
    """Provider 的运行时配置：描述 base_url 对应哪类 provider，以及该 provider 的 thinking 字段如何映射。"""

    name: str = Field(..., description="provider 标识：openai / ollama / glm")
    thinking_field: Optional[str] = Field(default=None, description="该 provider 使用的 thinking 字段名：reasoning_effort / thinking / None")
    thinking_disabled_value: Any = Field(default=None, description="当 reasoning_effort = off 时传给 LLM 的值")
    thinking_effort_values: Optional[Dict[str, Any]] = Field(default=None, description="low / medium / high 的取值映射")

    # ---------- factory ----------

    @classmethod
    def for_provider(cls, name: str) -> "ProviderConfig":
        """工厂：根据 provider 名称返回预置策略。"""
        name = (name or "").lower()
        if name == "ollama":
            return cls(
                name="ollama",
                thinking_field="reasoning_effort",
                thinking_disabled_value="none",
                thinking_effort_values={"low": "low", "medium": "medium", "high": "high"},
            )
        if name == "glm":
            return cls(
                name="glm",
                thinking_field="thinking",
                thinking_disabled_value={"type": "disabled"},
                thinking_effort_values={
                    "low": {"type": "enabled"},
                    "medium": {"type": "enabled"},
                    "high": {"type": "enabled"},
                },
            )
        # 默认 openai：reasoning_effort，off 时不传
        return cls(
            name="openai",
            thinking_field="reasoning_effort",
            thinking_disabled_value=None,
            thinking_effort_values={"low": "low", "medium": "medium", "high": "high"},
        )

    def resolve_extra(self, user_value: str) -> Dict[str, Any]:
        """把用户输入的 llm_thinking 值（off / low / medium / high / ""）解析成传给 LLM 的 extra kwargs。"""
        user_value = (user_value or "").strip().lower()
        if self.thinking_field is None:
            return {}
        if user_value in ("", "off"):
            if self.thinking_disabled_value is None:
                return {}
            return {self.thinking_field: self.thinking_disabled_value}
        mapped = (self.thinking_effort_values or {}).get(user_value, user_value)
        return {self.thinking_field: mapped}


# ========================================================================
# Pipeline 事件 — 统一协议
# ========================================================================

class PipelineEvent(BaseModel):
    """Pipeline 的统一事件对象：替代裸 dict 协议。"""

    model_config = ConfigDict(extra="allow")

    type: str = Field(..., description="事件类型：stage / delta / done / error")
    stage: Optional[str] = Field(default=None, description="阶段名：prepare / understand / preprocess / pick_type / generate / parse")
    status: Optional[str] = Field(default=None, description="阶段状态：start / done / skipped / error")
    content: Optional[str] = Field(default=None, description="delta 事件的 token 增量，或 error 事件的消息")
    data: Optional[Dict[str, Any]] = Field(default=None, description="阶段附属数据（可选）")

    # ---------- factory helpers ----------

    @classmethod
    def create_stage(cls, phase: str, status: str, label: str, **extra: Any) -> "PipelineEvent":
        return cls(type="stage", stage=phase, status=status, data={"label": label, **extra})

    @classmethod
    def create_delta(cls, token: str) -> "PipelineEvent":
        return cls(type="delta", content=token)

    @classmethod
    def create_done(cls, chart_type: str, option: Dict[str, Any], content: str,
                    raw_reply: str, parse_method: str, type_reason: str = "",
                    understanding: Optional[Dict[str, Any]] = None,
                    preprocess: Optional[Dict[str, Any]] = None) -> "PipelineEvent":
        import json
        return cls(
            type="done",
            data={
                "chart_type": chart_type,
                "type_reason": type_reason,
                "option": option,
                "code": f"""const option = {json.dumps(option, ensure_ascii=False, indent=2)};""",
                "content": content,
                "explanation": content,
                "raw_reply": raw_reply,
                "parse_method": parse_method,
                **({"understanding": understanding} if understanding else {}),
                **({"preprocess": preprocess} if preprocess else {}),
            },
        )

    @classmethod
    def create_error(cls, message: str, status: int = 500, raw_reply: Optional[str] = None) -> "PipelineEvent":
        return cls(type="error", content=message, data={"status": status, "raw_reply": raw_reply})
