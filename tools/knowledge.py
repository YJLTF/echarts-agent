#!/usr/bin/env python3
"""LangChain Tools 封装。"""
from typing import Any, Dict, Optional

from langchain.tools import Tool

from knowledge import get_knowledge_for_type, search_knowledge


def create_knowledge_tool() -> Tool:
    """创建 ECharts 知识库检索工具。"""

    def get_knowledge(chart_type: str) -> str:
        """根据图表类型获取 ECharts 配置指导。"""
        if not chart_type:
            return "请提供图表类型"
        result = get_knowledge_for_type(chart_type)
        if not result:
            return f"未找到类型 '{chart_type}' 的知识库内容"
        lines = []
        for section, content in result.items():
            lines.append(f"=== {section} ===")
            lines.append(content)
        return "\n".join(lines)

    return Tool(
        name="echarts_knowledge",
        func=get_knowledge,
        description=(
            "检索 ECharts 配置项知识库。根据图表类型返回该类型的配置指导，"
            "包括通用基础配置、tooltip、轴配置、图例等。适用于需要了解 ECharts 配置项细节时。"
        ),
    )


def create_search_knowledge_tool() -> Tool:
    """创建知识库搜索工具。"""

    def search(q: str) -> str:
        """根据关键词搜索知识库。"""
        if not q:
            return "请提供搜索关键词"
        result = search_knowledge(q)
        if not result:
            return f"未找到与 '{q}' 相关的知识库内容"
        lines = []
        for section, content in result.items():
            lines.append(f"=== {section} ===")
            lines.append(content)
        return "\n".join(lines)

    return Tool(
        name="search_knowledge",
        func=search,
        description=(
            "搜索 ECharts 知识库。根据关键词搜索相关的配置指导，"
            "可以搜索图表类型、配置项（如 tooltip、legend、axis）等。"
        ),
    )


def get_all_tools() -> list:
    """获取所有可用的工具列表。"""
    return [
        create_knowledge_tool(),
        create_search_knowledge_tool(),
    ]
