#!/usr/bin/env python3
"""数据预处理相关的 LangChain Tools。"""
from typing import Any, Dict

from langchain.tools import Tool

from data_preprocessing import preprocess_data


def create_preprocess_tool() -> Tool:
    """创建数据预处理工具。"""

    def preprocess(prompt: str, data: Dict[str, Any]) -> str:
        """根据用户 prompt 对数据进行预处理。

        Args:
            prompt: 用户的需求描述（如"只看销售额大于1000的"）
            data: 包含 rows/columns 等字段的数据字典

        Returns:
            预处理结果的 JSON 字符串
        """
        try:
            new_data, info = preprocess_data(prompt, data)
            result = {
                "data": new_data,
                "info": info,
            }
            import json
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return f"预处理失败：{e}"

    return Tool(
        name="data_preprocessor",
        func=preprocess,
        description=(
            "对上传的数据进行预处理。根据用户的自然语言需求（如过滤、转换、聚合等）"
            "自动识别并应用预处理规则。例如：'只看销售额大于1000的'、'按月份汇总'等。"
        ),
    )


def get_all_preprocess_tools() -> list:
    """获取所有数据预处理相关的工具。"""
    return [create_preprocess_tool()]
