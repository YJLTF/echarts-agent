#!/usr/bin/env python3
"""图表生成的 LCEL Chain（Pydantic 结构化输出版）。

核心抽象：``prompt | llm | ChartGenerationResponse``。
结构化输出失败时降级到手写解析（5 层兜底）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional, Tuple

from llm_client import get_llm_wrapper
from knowledge import get_knowledge_for_type
from prompts.chart_generation import build_chart_user_prompt
from output_parsers.schema import ChartGenerationResponse
from chains.base import StructuredLLMChain


_DEFAULT_SYSTEM_PROMPT = (
    "你是一个专业的 ECharts 图表生成助手，请根据用户需求生成符合规范的 ECharts 配置。"
)


def _build_system_prompt(cfg: Dict[str, Any]) -> str:
    """从 cfg 提取或使用默认 system prompt。"""
    return cfg.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT


# ========================================================================
# Chain 构造
# ========================================================================

def build_chart_generation_chain(cfg: Dict[str, Any]) -> StructuredLLMChain:
    """构建图表生成的 Chain（声明式的 ``prompt | llm | ChartGenerationResponse``）。

    底层由 :class:`StructuredLLMChain` 提供：

    - 优先走 ``ChatOpenAI.with_structured_output(ChartGenerationResponse)``
      让 LLM 直接返回 Pydantic 对象，类型安全
    - 失败时降级：普通 JSON mode → 不带 response_format → 手写 5 层解析
    """
    return StructuredLLMChain(
        cfg=cfg,
        system_prompt=_build_system_prompt(cfg),
        output_model=ChartGenerationResponse,
    )


# ========================================================================
# 便捷调用（非流式）
# ========================================================================

def generate_chart(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str = "bar",
    style_hint: Optional[Dict[str, Any]] = None,
    preprocess_info: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """生成图表的便捷函数（非流式）。

    Returns:
        ``(option, content, parse_method_or_error_message)``
    """
    chain = build_chart_generation_chain(cfg)

    # 组装用户输入
    knowledge = get_knowledge_for_type(chart_type)
    user_prompt = build_chart_user_prompt(
        prompt=prompt,
        data=data,
        chart_type=chart_type,
        style_hint=style_hint,
        knowledge=knowledge,
        preprocess_info=preprocess_info,
    )

    try:
        result = chain.invoke(user_prompt)
    except Exception as exc:
        return None, None, f"llm_error: {exc}"

    if isinstance(result, ChartGenerationResponse):
        # Pydantic 模型 → 最理想的输出，解析方法 = structured
        return result.option, result.content, "structured"

    if isinstance(result, dict):
        # 降级：从 dict 中抽取
        return (
            result.get("option"),
            result.get("content"),
            result.get("parse_method", "dict_fallback"),
        )

    # 字符串 → 交给 chart_parser 的 5 层兜底
    from output_parsers.chart_parser import parse_chart_response as _parse
    option, content, parse_method, error = _parse(result if isinstance(result, str) else repr(result))
    if error:
        return None, None, error
    return option, content, parse_method or "raw_string"


# ========================================================================
# 便捷调用（流式）
# ========================================================================

def generate_chart_stream(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str = "bar",
    style_hint: Optional[Dict[str, Any]] = None,
    preprocess_info: Optional[Dict[str, Any]] = None,
) -> Iterator[str]:
    """流式生成图表：逐 token 产出 content 文本。

    前端消费完所有 chunk 后，再在自己那侧做 JSON 解析（或再调一次非流式）。
    """
    wrapper = get_llm_wrapper(cfg)

    # 组装用户输入
    knowledge = get_knowledge_for_type(chart_type)
    user_prompt = build_chart_user_prompt(
        prompt=prompt,
        data=data,
        chart_type=chart_type,
        style_hint=style_hint,
        knowledge=knowledge,
        preprocess_info=preprocess_info,
    )

    messages = [
        {"role": "system", "content": _build_system_prompt(cfg)},
        {"role": "user", "content": user_prompt},
    ]

    for chunk in wrapper.call_llm_stream(
        messages=messages,
        max_tokens=cfg.get("max_tokens", 2048),
        temperature=cfg.get("temperature", 0.7),
        reasoning_effort=cfg.get("llm_thinking"),
    ):
        if chunk:
            yield chunk
