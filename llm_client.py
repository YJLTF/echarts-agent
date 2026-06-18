#!/usr/bin/env python3
"""LangChain LLM 客户端封装。

提供统一的 ChatOpenAI 初始化，支持流式/非流式调用，
保留原有的 provider 检测和 thinking 字段处理逻辑。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Type, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from output_parsers.schema import ProviderConfig

T = TypeVar("T", bound=BaseModel)

# ------------------------- Provider 检测（向后兼容） -------------------------


def get_provider_config(base_url: str, provider: Optional[str] = None) -> ProviderConfig:
    """根据 Base URL 返回 Provider 策略配置对象。

    嗅探规则：

    - 命中 ``:11434``（Ollama 默认端口）或路径里含 ``/ollama`` → ``"ollama"``
    - 命中 ``bigmodel.cn`` / ``zhipu`` / ``zhipuai`` → ``"glm"``（智谱 GLM-4.5+）
    - 其它 → ``"openai"``（含 OpenAI / DeepSeek / DashScope 等 OpenAI 兼容服务）
    """
    u = (base_url or "").lower()
    name = (provider or "").lower() or _sniff_provider(u)
    return ProviderConfig.for_provider(name)


def _sniff_provider(u: str) -> str:
    """根据 Base URL 嗅探 provider 名称。"""
    if ":11434" in u or "/ollama" in u:
        return "ollama"
    if "bigmodel.cn" in u or "zhipuai" in u or "zhipu" in u:
        return "glm"
    return "openai"


def _detect_provider(base_url: str) -> str:
    """返回 provider 名字符串（兼容旧接口）。"""
    return get_provider_config(base_url).name


def resolve_extra_kwargs(base_url: str, user_value: Optional[str], provider: Optional[str] = None) -> Dict[str, Any]:
    """把用户输入的 ``llm_thinking`` 值解析成传给 LLM 的 extra kwargs。

    基于 ``ProviderConfig`` 的策略配置返回 ``{"reasoning_effort": ...}`` 或 ``{"thinking": ...}``。
    """
    config = get_provider_config(base_url, provider)
    return config.resolve_extra(user_value or "")


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
        # 按 (streaming, frozenset(overrides.items())) 缓存 ChatOpenAI 实例，避免每次调用重建。
        # 实测每次 ChatOpenAI(**kwargs) 都会触发 httpx Client 创建（百毫秒级）。
        self._llm_cache: Dict[Any, ChatOpenAI] = {}

    def _build_llm(self, stream: bool = False, **overrides) -> ChatOpenAI:
        """构建或返回缓存的 ChatOpenAI 实例（按 stream + overrides 缓存）。"""
        cache_key = (stream, frozenset(overrides.items()) if overrides else None)
        cached = self._llm_cache.get(cache_key)
        if cached is not None:
            return cached
        llm = ChatOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            streaming=stream,
            timeout=120,
            **overrides,
        )
        self._llm_cache[cache_key] = llm
        return llm

    @staticmethod
    def to_lc_messages(messages: List[Dict[str, str]]) -> List[BaseMessage]:
        """把 OpenAI 风格的 ``[{"role":..., "content":...}]`` 转成 LangChain 消息列表。"""
        out: List[BaseMessage] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                out.append(SystemMessage(content=content))
            else:
                out.append(HumanMessage(content=content))
        return out

    def _bind_common(
        self,
        llm: ChatOpenAI,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Runnable:
        """把通用参数（max_tokens / temperature / response_format / thinking）绑到 LLM 上。"""
        bind_kwargs: Dict[str, Any] = {
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        # response_format (json_schema / json_object) 只对 OpenAI 兼容 provider 有效；
        # Ollama / GLM 等不支持，发送会触发 502 — 跳过
        if response_format is not None and self.provider not in ("ollama", "glm"):
            bind_kwargs["response_format"] = response_format
        # reasoning_effort / thinking 等 provider 特有字段通过 extra_body 传递
        extra_body = resolve_extra_kwargs(
            self.base_url, reasoning_effort or self.reasoning_effort, self.provider
        )
        if extra_body:
            bind_kwargs["extra_body"] = extra_body
        return llm.bind(**bind_kwargs)

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
        bound = self._bind_common(
            llm,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )
        response = bound.invoke(self.to_lc_messages(messages))

        content = ""
        reasoning = ""
        if isinstance(response, AIMessage):
            content = response.content or ""
            # 从 additional_kwargs 中获取 reasoning / reasoning_content
            reasoning = (
                response.additional_kwargs.get("reasoning")
                or response.additional_kwargs.get("reasoning_content")
                or ""
            )

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
        llm = self._build_llm(stream=True)
        bound = self._bind_common(
            llm,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )
        try:
            for event in bound.stream(self.to_lc_messages(messages)):
                if isinstance(event, AIMessage):
                    content = event.content or ""
                    if content:
                        yield content
        except Exception as e:
            raise RuntimeError(f"LLM 流式调用失败：{e}") from e

    def structured(
        self,
        schema: Type[T],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Runnable:
        """返回一个 LangChain ``Runnable``，调用后直接返回 ``schema`` 类型的 Pydantic 对象。

        内部用 :meth:`ChatOpenAI.with_structured_output`：

        - 自动在 system prompt 注入 JSON schema 说明
        - 对 OpenAI 兼容 provider 走 ``response_format=json_schema`` 严格模式
        - 对 Ollama / GLM 走 tool-calling 兼容模式（不同 LangChain 版本有差异）
        - 用 Pydantic 校验返回值，失败时自动重试
        """
        from langchain_core.language_models import BaseChatModel
        llm = self._build_llm(stream=False)
        bound = self._bind_common(
            llm,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=None,  # with_structured_output 内部自行设置
            reasoning_effort=reasoning_effort,
        )
        assert isinstance(bound, BaseChatModel), "with_structured_output requires a chat model"
        return bound.with_structured_output(schema)


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


_ALLOWED_CHART_TYPES = frozenset({
    "bar", "line", "pie", "scatter", "radar", "gauge", "funnel",
    "candlestick", "heatmap", "sunburst", "treemap", "sankey",
    "boxplot", "pictorialbar", "effectscatter",
})


def _lc_messages_to_dicts(messages: List[Any]) -> List[Dict[str, str]]:
    """LangChain 消息列表 → ``[{"role","content"}]`` dict 列表。"""
    out: List[Dict[str, str]] = []
    role_map = ((SystemMessage, "system"), (HumanMessage, "user"), (AIMessage, "assistant"))
    for m in messages:
        for klass, role in role_map:
            if isinstance(m, klass):
                out.append({"role": role, "content": m.content})
                break
    return out


def pick_chart_type(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    hint: str,
) -> tuple[str, str]:
    """让 LLM 根据需求和数据推荐一个图表类型。返回 ``(chart_type, reason)``。"""
    if hint:
        return hint, "用户指定图表类型。"

    from output_parsers.schema import ChartTypeRecommendation
    from prompts import format_data_for_type_prompt, chart_type_prompt_template

    tmpl_params = format_data_for_type_prompt(prompt, data, list(_ALLOWED_CHART_TYPES))
    rendered = chart_type_prompt_template().format_messages(**tmpl_params)
    lc_messages = _lc_messages_to_dicts(rendered)

    try:
        structured = get_llm_wrapper(cfg).structured(
            ChartTypeRecommendation,
            max_tokens=200,
            temperature=0.3,
            reasoning_effort=cfg.get("reasoning_effort") or None,
        )
        result = structured.invoke(lc_messages)
        chart_type = (result.chart_type or "").lower().strip() or "bar"
        if chart_type not in _ALLOWED_CHART_TYPES:
            chart_type = "bar"
        return chart_type, result.reason or "自动选择。"
    except Exception:
        # structured 失败（不支持 / 校验失败）→ 走 fallback：用普通 call_llm + 取首行
        return _pick_chart_type_fallback(cfg, lc_messages)


def _pick_chart_type_fallback(cfg: Dict[str, Any], messages: List[Dict[str, str]]) -> tuple[str, str]:
    """``pick_chart_type`` 的兼容回退：直接用 ChatPromptTemplate 渲染好的消息发出去，
    模型回首行 chart_type + 第二行理由。
    """
    reply = call_llm(
        cfg,
        messages=messages,
        max_tokens=200,
        temperature=0.3,
        reasoning_effort=cfg.get("reasoning_effort") or None,
    )
    lines = [ln.strip() for ln in reply.strip().splitlines() if ln.strip()]
    chart_type = lines[0].lower() if lines else "bar"
    if chart_type not in _ALLOWED_CHART_TYPES:
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
    """向后兼容的薄封装：图表生成 user prompt 由 :mod:`prompts.build_chart_user_prompt` 统一构建。"""
    from prompts import build_chart_user_prompt
    return build_chart_user_prompt(prompt, data, chart_type, style_hint, knowledge)
