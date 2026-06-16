#!/usr/bin/env python3
"""LangChain LLM 客户端封装。

提供统一的 ChatOpenAI 初始化，支持流式/非流式调用，
保留原有的 provider 检测和 thinking 字段处理逻辑。
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request
import urllib.error
from typing import Any, Dict, Iterator, List, Optional, Union

from langchain_openai import ChatOpenAI

try:
    from output_parsers.schema import ProviderConfig
    _HAS_PYDANTIC = True
except Exception:
    _HAS_PYDANTIC = False

# ------------------------- Provider 检测（向后兼容） -------------------------


def get_provider_config(base_url: str, provider: Optional[str] = None) -> "ProviderConfig":
    """根据 Base URL 返回 Provider 策略配置对象。

    返回值包含 ``thinking_field`` / ``thinking_disabled_value`` / ``thinking_effort_values``
    三个属性，统一描述该 provider 的 thinking 字段如何映射。

    嗅探规则：

    - 命中 ``:11434``（Ollama 默认端口）或路径里含 ``/ollama`` → ``"ollama"``
    - 命中 ``bigmodel.cn`` / ``zhipu`` / ``zhipuai`` → ``"glm"``（智谱 GLM-4.5+）
    - 其它 → ``"openai"``（含 OpenAI / DeepSeek / DashScope 等 OpenAI 兼容服务）
    """
    u = (base_url or "").lower()
    name = provider or "openai"
    if provider is None:
        if ":11434" in u or "/ollama" in u:
            name = "ollama"
        elif "bigmodel.cn" in u or "zhipuai" in u or "zhipu" in u:
            name = "glm"
        else:
            name = "openai"
    if _HAS_PYDANTIC:
        return ProviderConfig.for_provider(name)
    # Pydantic 不可用时，返回一个兼容的命名 tuple
    _DummyProvider = type("ProviderConfig", (), {"name": name})
    return _DummyProvider()


def _detect_provider(base_url: str) -> str:
    """与旧接口兼容：返回 provider 名字符串。

    新代码请使用 :func:`get_provider_config` 返回带策略的 ``ProviderConfig``。
    """
    return get_provider_config(base_url).name if _HAS_PYDANTIC else _legacy_detect(base_url)


def _legacy_detect(base_url: str) -> str:
    u = (base_url or "").lower()
    if ":11434" in u or "/ollama" in u:
        return "ollama"
    if "bigmodel.cn" in u or "zhipuai" in u or "zhipu" in u:
        return "glm"
    return "openai"


def resolve_extra_kwargs(base_url: str, user_value: Optional[str], provider: Optional[str] = None) -> Dict[str, Any]:
    """把用户输入的 ``llm_thinking`` 值解析成传给 LLM 的 extra kwargs。

    基于 ``ProviderConfig`` 的策略配置返回 ``{"reasoning_effort": ...}`` 或 ``{"thinking": ...}``。
    """
    if not _HAS_PYDANTIC:
        # 降级：手写的旧逻辑
        return _legacy_resolve_extra(base_url, user_value, provider)
    config = get_provider_config(base_url, provider)
    return config.resolve_extra(user_value)


def _legacy_resolve_extra(base_url: str, user_value: Optional[str], provider: Optional[str] = None) -> Dict[str, Any]:
    p = (provider or _legacy_detect(base_url)).lower()
    v = (user_value or "").strip().lower()
    if v in ("", "off"):
        if p == "ollama":
            return {"reasoning_effort": "none"}
        if p == "glm":
            return {"thinking": {"type": "disabled"}}
        return {}
    if p == "glm":
        return {"thinking": {"type": "enabled"}}
    return {"reasoning_effort": v if v in ("low", "medium", "high") else "medium"}


# ------------------------- LangChain ChatOpenAI 封装 -------------------------

class ChatOpenAIWrapper:
    """对 langchain_openai.ChatOpenAI 的封装，保留原有 thinking 字段处理逻辑。

    支持：
    - 流式/非流式调用
    - reasoning_effort / thinking 字段
    - response_format 结构化输出
    - 保留原有 call_llm / call_llm_stream 接口
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        reasoning_effort: str = "",
        **kwargs,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.provider = _detect_provider(self.base_url)
        self._llm: Optional[ChatOpenAI] = None

    def _build_llm(self, stream: bool = False, **overrides) -> ChatOpenAI:
        """构建或返回缓存的 ChatOpenAI 实例。"""
        if self._llm is None or overrides:
            kwargs = {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "streaming": stream,
                **overrides,
            }
            return ChatOpenAI(**kwargs)
        return self._llm

    def _build_payload(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[Dict[str, Any]],
        reasoning_effort: Optional[str],
        stream: bool,
    ) -> Dict[str, Any]:
        """构建请求 payload（用于手动 HTTP 调用或验证）。"""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        thinking = resolve_extra_kwargs(self.base_url, reasoning_effort, self.provider)
        if thinking:
            payload.update(thinking)
        if stream:
            payload["stream"] = True
        return payload

    def call_llm(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """调用大模型（一次性）。**只返回 content 文本**。"""
        content, _ = self.call_llm_raw(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )
        return content

    def call_llm_raw(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> tuple[str, str]:
        """调用大模型（一次性），返回 ``(content, reasoning)``。"""
        llm = self._build_llm(stream=False)
        max_t = max_tokens if max_tokens is not None else self.max_tokens
        temp = temperature if temperature is not None else self.temperature

        # 构建 extra_body
        extra_body: Dict[str, Any] = resolve_extra_kwargs(
            self.base_url, reasoning_effort or self.reasoning_effort, self.provider
        )

        # 构建 chat messages
        from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
        langchain_messages: List[BaseMessage] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                langchain_messages.append(SystemMessage(content=content))
            else:
                langchain_messages.append(HumanMessage(content=content))

        response = llm.invoke(
            langchain_messages,
            config={"max_tokens": max_t, "temperature": temp},
        )

        content = ""
        reasoning = ""
        if isinstance(response, AIMessage):
            content = response.content or ""
            # 尝试从 additional_kwargs 中获取 reasoning
            reasoning = response.additional_kwargs.get("reasoning") or ""

        return content, reasoning

    def call_llm_stream(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Iterator[str]:
        """流式调用大模型，逐 chunk 产出 content。"""
        max_t = max_tokens if max_tokens is not None else self.max_tokens
        temp = temperature if temperature is not None else self.temperature

        from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
        langchain_messages: List[BaseMessage] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                langchain_messages.append(SystemMessage(content=content))
            else:
                langchain_messages.append(HumanMessage(content=content))

        llm = self._build_llm(stream=True)
        for event in llm.stream(langchain_messages, config={"max_tokens": max_t, "temperature": temp}):
            if isinstance(event, AIMessage):
                content = event.content or ""
                if content:
                    yield content

    @property
    def llm(self) -> ChatOpenAI:
        """获取底层的 ChatOpenAI 实例（延迟初始化）。"""
        if self._llm is None:
            self._llm = self._build_llm(stream=False)
        return self._llm


# ------------------------- 兼容旧接口的函数 -------------------------

# 全局配置缓存
_llm_wrapper_cache: Dict[str, ChatOpenAIWrapper] = {}


def get_llm_wrapper(cfg: Dict[str, Any]) -> ChatOpenAIWrapper:
    """根据配置获取或创建 LLM Wrapper 实例。"""
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""

    cache_key = f"{base_url}:{api_key}:{model}"
    if cache_key not in _llm_wrapper_cache:
        _llm_wrapper_cache[cache_key] = ChatOpenAIWrapper(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=float(cfg.get("temperature") or 0.7),
            max_tokens=int(cfg.get("max_tokens") or 2048),
            reasoning_effort=cfg.get("reasoning_effort") or "",
        )
    return _llm_wrapper_cache[cache_key]


# 兼容旧接口的函数（保留原有 llm_client.py 的 API）
def call_llm(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """调用大模型（一次性）。**只返回 content 文本**。"""
    wrapper = get_llm_wrapper(cfg)
    return wrapper.call_llm(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
    )


def call_llm_raw(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, str]:
    """调用大模型（一次性），返回 ``(content, reasoning)``。"""
    wrapper = get_llm_wrapper(cfg)
    return wrapper.call_llm_raw(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
    )


def call_llm_stream(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
):
    """流式调用大模型，逐 chunk 产出 content。"""
    wrapper = get_llm_wrapper(cfg)
    return wrapper.call_llm_stream(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
    )


# ------------------------- 统计相关函数（保留原有） -------------------------

def _percentile(sorted_nums: List[float], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认行为一致）。"""
    if not sorted_nums:
        return 0.0
    n = len(sorted_nums)
    if n == 1:
        return sorted_nums[0]
    k = (n - 1) * q
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_nums[f]
    return sorted_nums[f] + (sorted_nums[c] - sorted_nums[f]) * (k - f)


def compute_column_stats(
    rows: List[Dict[str, Any]],
    column_names: List[str],
) -> str:
    """给一份行数据算每列的紧凑统计，喂给 LLM 让它对「全量」有概念。"""
    if not rows or not column_names or len(rows) < 20:
        return ""

    def _try_float(v: Any) -> Optional[float]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", "").replace(" ", "")
        if s in ("", "-", "—"):
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    def _fmt_num(x: float) -> str:
        if x == int(x) and abs(x) < 1e15:
            return f"{int(x):,}"
        return f"{x:,.2f}"

    show_percentiles = len(rows) >= 100
    lines: List[str] = []
    for col in column_names:
        if not col:
            continue
        vals = [r.get(col) for r in rows]
        non_null = [v for v in vals if v is not None and v != ""]
        nulls = len(vals) - len(non_null)

        if not non_null:
            lines.append(f"- {col}: 全空")
            continue

        nums: List[float] = []
        for v in non_null:
            fv = _try_float(v)
            if fv is not None:
                nums.append(fv)
        is_numeric = len(nums) >= len(non_null) * 0.7
        null_part = f", {nulls} 空" if nulls else ""

        if is_numeric:
            nums_sorted = sorted(nums)
            distinct = len(set(nums_sorted))
            lo, hi = nums_sorted[0], nums_sorted[-1]
            range_str = f"={_fmt_num(lo)}" if lo == hi else f"{_fmt_num(lo)}–{_fmt_num(hi)}"
            mean = sum(nums) / len(nums)
            parts = [f"范围 {range_str}", f"均值 {_fmt_num(mean)}", f"{distinct} distinct"]
            if show_percentiles:
                med = _percentile(nums_sorted, 0.5)
                p25 = _percentile(nums_sorted, 0.25)
                p75 = _percentile(nums_sorted, 0.75)
                iqr = p75 - p25
                parts.append(f"中位数 {_fmt_num(med)}")
                parts.append(f"p25-p75 {_fmt_num(p25)}–{_fmt_num(p75)}")
                if iqr > 0 and distinct > 4:
                    parts.append(f"IQR {_fmt_num(iqr)}")
            line = f"- {col} (数字, {len(non_null)} 值{null_part}): " + ", ".join(parts)
        else:
            from collections import Counter
            cnt = Counter(str(v) for v in non_null)
            distinct = len(cnt)
            top = cnt.most_common(3)
            top_str = ", ".join(f'"{k}"({n})' for k, n in top)
            line = f"- {col} (文本, {len(non_null)} 值{null_part}): {distinct} distinct, top: {top_str}"

        if len(line) > 240:
            line = line[:237] + "..."
        lines.append(line)

    return "\n".join(lines)


def _detect_provider_for_client(base_url: str) -> str:
    """暴露给外部的 provider 检测函数。"""
    return _detect_provider(base_url)


def pick_chart_type(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    hint: str,
):
    """让 LLM 根据需求和数据推荐一个图表类型。"""
    if hint:
        return hint, "用户指定图表类型。"

    data_desc = ""
    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            cols = ", ".join(str(c.get("name") or "") for c in raw_cols)
        else:
            cols = ", ".join(str(c) for c in raw_cols)
        sample = json.dumps(data["rows"][:3], ensure_ascii=False)
        extra = ""
        if data.get("summary"):
            extra = f"\n数据集摘要：{data['summary']}"
        data_desc = f"\n数据字段：{cols}\n样例：{sample}\n行数：{len(data['rows'])}{extra}"

    system = (
        "你是一个专业的数据可视化助手，请根据用户的需求与数据，推荐一个最合适的 ECharts 图表类型。"
        "请只回复一个英文单词，从以下类型中选择：bar, line, pie, scatter, radar, gauge, funnel, candlestick, heatmap, sunburst, treemap, sankey, boxplot, pictorialBar, effectScatter。"
        "随后用一行中文简短解释选择理由。"
        "示例输出：\nbar\n用于比较不同类别之间的数值大小，适合该场景的分类数据对比。"
    )
    user = f"用户需求：{prompt or '根据提供的数据自动生成合适的图表'}{data_desc}\n\n请给出推荐图表类型与理由。"
    reply = call_llm(
        cfg,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=200,
        temperature=0.3,
        reasoning_effort=cfg.get("reasoning_effort") or None,
    )
    lines = [ln.strip() for ln in reply.strip().splitlines() if ln.strip()]
    chart_type = lines[0].lower() if lines else "bar"
    allowed = {"bar","line","pie","scatter","radar","gauge","funnel","candlestick","heatmap","sunburst","treemap","sankey","boxplot","pictorialbar","effectscatter"}
    if chart_type not in allowed:
        chart_type = "bar"
    reason = " ".join(lines[1:]) or "自动选择。"
    return chart_type, reason


def build_chart_prompt(
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str,
    style_hint: Optional[Dict[str, Any]],
    knowledge: Dict[str, Any],
) -> str:
    pieces = []
    pieces.append(f"请使用 Apache ECharts 绘制一张「{chart_type}」类型图表。")
    if prompt:
        pieces.append(f"用户需求：{prompt}")

    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            column_names = [str(c.get("name") or "") for c in raw_cols]
            schema_lines = [
                f"- {c.get('name')} ({c.get('type','string')}/{c.get('role','value')}): {c.get('description','')}"
                for c in raw_cols
            ]
            pieces.append("数据 schema：\n" + "\n".join(schema_lines))
        else:
            column_names = [str(c) for c in raw_cols]
        rows = data["rows"]
        pieces.append("数据字段：" + ", ".join(column_names))

        stats_text = compute_column_stats(rows, column_names)
        if stats_text:
            pieces.append(f"【数据统计摘要 (共 {len(rows)} 行)】\n{stats_text}")

        if len(rows) > 100:
            shown = rows[:100]
            pieces.append(f"数据共 {len(rows)} 行，仅发送前 100 行用于演示；"
                          f"请在生成代码时按相同字段保留完整结构：")
        else:
            shown = rows
        pieces.append("数据 JSON：")
        pieces.append(json.dumps(shown, ensure_ascii=False))

        if data.get("summary"):
            pieces.append(f"数据集摘要：{data['summary']}")
        if data.get("notes") and data.get("notes") not in ("", "无"):
            pieces.append(f"数据整理说明：{data['notes']}")

    if style_hint:
        pieces.append(f"样式偏好：{json.dumps(style_hint, ensure_ascii=False)}")

    pieces.append(
        "请严格按 response_format 给定的 JSON schema 输出（不要写 Markdown 代码块、不要任何前后缀文字），"
        "其中：\n"
        "1) option 是完整的 ECharts 配置对象（title/tooltip/legend/grid/xAxis/yAxis/series/color 等），"
        "series.data 填入真实数值；\n"
        "2) xAxis/yAxis/legend 的内容与数据列名一致；\n"
        "3) 标题/副标题可以留空；\n"
        "4) 禁止 JS 函数字面量（JSON 不支持函数），自定义 formatter 用 ECharts 字符串模板"
        "（如 '{b}: {c}'），自定义配色用 series 顶层 color: [...] 数组或省略；\n"
        "5) content 字段填 30-80 字中文文字解释该图表表达的核心信息（最大/最小/趋势/对比），"
        "不要复述数据，不要 markdown 格式。"
    )

    if knowledge:
        pieces.append("【ECharts 配置项指导】")
        for section_name, content in knowledge.items():
            pieces.append(f"-- {section_name} --")
            pieces.append(content)

    return "\n\n".join(pieces)
