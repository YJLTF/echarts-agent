#!/usr/bin/env python3
"""图表类型选择相关的 LangChain Tools。"""
import json
from typing import Any, Dict, Optional

from langchain.tools import Tool

from llm_client import get_llm_wrapper


def create_chart_type_selector_tool() -> Tool:
    """创建图表类型选择工具。"""

    def select_chart_type(
        cfg: Dict[str, Any],
        prompt: str,
        data: Optional[Dict[str, Any]] = None,
        hint: str = "",
    ) -> str:
        """根据用户需求和数据选择合适的图表类型。

        Args:
            cfg: LLM 配置字典
            prompt: 用户的需求描述
            data: 数据字典（包含 rows/columns 等）
            hint: 用户指定的图表类型（可选）

        Returns:
            JSON 字符串，包含 chart_type 和 reason
        """
        if hint:
            result = {
                "chart_type": hint,
                "reason": "用户指定图表类型",
            }
            return json.dumps(result, ensure_ascii=False)

        try:
            from llm_client import pick_chart_type
            chart_type, reason = pick_chart_type(cfg, prompt, data, hint)
            result = {
                "chart_type": chart_type,
                "reason": reason,
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"选择图表类型失败：{e}"})

    return Tool(
        name="chart_type_selector",
        func=select_chart_type,
        description=(
            "根据用户需求和数据特征选择最合适的 ECharts 图表类型。"
            "如果用户已经指定了图表类型（通过 hint 参数），则直接返回用户的选择。"
        ),
    )


def get_all_chart_tools() -> list:
    """获取所有图表相关的工具。"""
    return [create_chart_type_selector_tool()]
