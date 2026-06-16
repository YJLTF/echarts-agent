#!/usr/bin/env python3
"""数据理解与整理的 LCEL Chain。"""
from typing import Any, Dict, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

from llm_client import get_llm_wrapper, compute_column_stats
from prompts.data_understanding import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    format_data_understanding_input,
)
from output_parsers.chart_parser import parse_data_understanding_response


def build_data_understanding_chain(cfg: Dict[str, Any]):
    """构建数据理解与整理的 LCEL 链。

    Args:
        cfg: LLM 配置字典，包含 base_url, api_key, model 等

    Returns:
        一个 Runnable 对象，输入数据理解参数，输出整理后的数据
    """
    wrapper = get_llm_wrapper(cfg)

    def format_input(inputs: dict) -> dict:
        """格式化输入参数。"""
        import json
        cols = inputs.get("cols", [])
        if isinstance(cols, list):
            cols_line = ", ".join(str(c) for c in cols)
        else:
            cols_line = str(cols)

        rows = inputs.get("rows", [])
        sample_rows = rows[:8]
        sample_text = json.dumps(sample_rows, ensure_ascii=False, indent=2) if sample_rows else "(无)"

        col_names = cols if isinstance(cols, list) else [cols]
        stats_text = compute_column_stats(rows, col_names) if rows else ""

        return {
            "hint": inputs.get("hint", ""),
            "cols": cols_line,
            "count": len(rows),
            "sample_n": len(sample_rows),
            "sample": sample_text,
            "stats": stats_text or "（样本太小，无需统计）",
            "raw_chars": inputs.get("raw_chars", 6000),
            "raw": (inputs.get("raw") or "").strip()[:6000] if inputs.get("raw") else "(无)",
        }

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", USER_PROMPT_TEMPLATE),
    ])

    def parse_output(raw_output: str) -> dict:
        """解析 LLM 输出。"""
        obj, err = parse_data_understanding_response(raw_output)
        if err:
            return {
                "success": False,
                "error": err,
                "raw_reply": raw_output,
            }
        return {
            "success": True,
            "columns": obj.get("columns", []),
            "rows": obj.get("rows", []),
            "summary": obj.get("summary", ""),
            "notes": obj.get("notes", ""),
            "raw_reply": raw_output,
        }

    chain = (
        RunnableLambda(format_input)
        | prompt
        | wrapper.llm
        | StrOutputParser()
        | RunnableLambda(parse_output)
    )

    return chain


def invoke_data_understanding(
    cfg: Dict[str, Any],
    parsed: Dict[str, Any],
    user_hint: str = "",
    raw_preview: str = "",
) -> Dict[str, Any]:
    """调用数据理解链的便捷函数。

    Args:
        cfg: LLM 配置
        parsed: 代码解析后的数据
        user_hint: 用户提示
        raw_preview: 原始数据预览

    Returns:
        整理后的数据字典
    """
    chain = build_data_understanding_chain(cfg)

    cols = parsed.get("columns", [])
    rows = parsed.get("rows", [])

    input_data = {
        "cols": cols,
        "rows": rows,
        "hint": user_hint,
        "raw": raw_preview,
    }

    result = chain.invoke(input_data)

    if result.get("success"):
        return {
            "columns": result["columns"],
            "column_names": [c["name"] for c in result["columns"]],
            "rows": result["rows"],
            "count": len(result["rows"]),
            "summary": result.get("summary", ""),
            "notes": result.get("notes", ""),
            "understand_method": "llm",
            "understand_error": None,
            "raw_reply": result.get("raw_reply", ""),
        }
    else:
        # Fallback to original parsed data
        return {
            **parsed,
            "understand_method": "fallback",
            "understand_error": result.get("error", "unknown"),
        }
