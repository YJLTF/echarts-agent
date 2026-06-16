#!/usr/bin/env python3
"""数据理解与整理的 LCEL Chain（Pydantic 结构化输出版）。

用 ``chains.base.StructuredLLMChain`` 装配声明式的 ``prompt | llm | pydantic`` 链；
用 ``output_parsers.schema.DataUnderstandingResponse`` 保证输出类型安全。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from llm_client import get_llm_wrapper, compute_column_stats
from prompts.data_understanding import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    format_data_understanding_input,
)
from output_parsers.schema import ColumnSchema, DataUnderstandingResponse
from chains.base import StructuredLLMChain, build_llm


# ========================================================================
# 输入格式化
# ========================================================================

def _format_input_for_llm(
    cols: List[str],
    rows: List[Dict[str, Any]],
    user_hint: str = "",
    raw_text: str = "",
) -> str:
    """把代码解析结果 + 用户提示格式化成给 LLM 的 user prompt 文本。

    复用 :func:`prompts.data_understanding.format_data_understanding_input` 得到
    template 变量 dict，再手工填充 ``USER_PROMPT_TEMPLATE``。
    """
    # 先用 format_data_understanding_input 得到 template 变量 dict
    data = format_data_understanding_input({
        "cols": cols,
        "rows": rows,
        "hint": user_hint,
        "raw": raw_text,
    })

    return USER_PROMPT_TEMPLATE.format(**data)


# ========================================================================
# Chain 构造
# ========================================================================

def build_data_understanding_chain(cfg: Dict[str, Any]) -> StructuredLLMChain:
    """构建数据理解与整理的 Chain。

    内部使用 :class:`StructuredLLMChain`：

    - system prompt 使用 :data:`prompts.data_understanding.SYSTEM_PROMPT`
    - 输出模型绑定到 :class:`output_parsers.schema.DataUnderstandingResponse`
    - 先尝试 ``ChatOpenAI.with_structured_output(DataUnderstandingResponse)``，
      失败时降级到手写解析
    """
    return StructuredLLMChain(
        cfg=cfg,
        system_prompt=SYSTEM_PROMPT,
        output_model=DataUnderstandingResponse,
    )


# ========================================================================
# 便捷调用
# ========================================================================

def invoke_data_understanding(
    cfg: Dict[str, Any],
    parsed: Dict[str, Any],
    user_hint: str = "",
    raw_preview: str = "",
) -> Dict[str, Any]:
    """调用数据理解链的便捷函数。

    Args:
        cfg: LLM 配置
        parsed: 代码解析后的数据（包含 ``columns`` / ``rows``）
        user_hint: 用户额外提示
        raw_preview: 原始数据预览（用于 LLM 二次理解）

    Returns:
        与旧接口兼容的 dict：``{columns, column_names, rows, count,
        summary, notes, understand_method, understand_error, raw_reply?}``
    """
    # 1) 准备输入
    cols_raw = parsed.get("columns") or []
    if isinstance(cols_raw, list) and cols_raw and isinstance(cols_raw[0], dict):
        # LLM 整理后的 columns：[{name, type, role, description}, ...]
        col_names = [c.get("name") for c in cols_raw]
    else:
        # 代码解析的 columns：["col1", "col2", ...]
        col_names = [str(c) for c in cols_raw]

    rows = parsed.get("rows") or []
    user_text = _format_input_for_llm(col_names, rows, user_hint, raw_preview)

    # 2) 调用 Chain
    chain = build_data_understanding_chain(cfg)
    try:
        result = chain.invoke(user_text)
    except Exception as exc:
        # LLM 调不通 → 直接 fallback
        return {
            **parsed,
            "understand_method": "fallback",
            "understand_error": f"llm_error: {exc}",
        }

    # 3) 解析结果
    if isinstance(result, DataUnderstandingResponse):
        # Pydantic 模型直接使用
        columns_dicts = [c.model_dump() for c in result.columns]
        return {
            "columns": columns_dicts,
            "column_names": [c["name"] for c in columns_dicts],
            "rows": result.rows,
            "count": len(result.rows),
            "summary": result.summary or "",
            "notes": result.notes or "",
            "understand_method": "llm",
            "understand_error": None,
        }

    # 非 Pydantic（字符串/其它）时，降级到旧接口格式
    if isinstance(result, dict):
        columns = result.get("columns", [])
        if columns and isinstance(columns[0], dict):
            col_names_out = [c.get("name") for c in columns]
        else:
            col_names_out = [str(c) for c in columns]
        return {
            "columns": columns,
            "column_names": col_names_out,
            "rows": result.get("rows", rows),
            "count": len(result.get("rows", rows)),
            "summary": result.get("summary", ""),
            "notes": result.get("notes", ""),
            "understand_method": "llm" if result.get("success", True) else "fallback",
            "understand_error": None if result.get("success", True) else result.get("error"),
            "raw_reply": result.get("raw_reply", ""),
        }

    # 最差 fallback：字符串内容，直接复用原始解析
    return {
        **parsed,
        "understand_method": "fallback",
        "understand_error": "unexpected_output_type",
        "raw_reply": str(result)[:2000] if isinstance(result, str) else repr(result)[:2000],
    }
