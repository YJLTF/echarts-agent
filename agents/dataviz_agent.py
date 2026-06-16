#!/usr/bin/env python3
"""DataViz Agent - 基于 LangChain 的自主决策智能体。

注意：此模块需要 langchain >= 0.1.0 且 API 可能因版本而异。
如遇导入错误，请确保已安装兼容版本的 langchain。
"""
from typing import Any, Dict, List, Optional

from llm_client import get_llm_wrapper


# Agent 系统提示
DATAVIZ_SYSTEM_PROMPT = """你是一个专业的数据可视化智能助手，擅长帮助用户分析和可视化数据。

你的能力包括：
1. 理解用户上传的数据（CSV、Excel、JSON 等）
2. 根据用户需求选择最合适的图表类型
3. 调用 ECharts 知识库获取配置指导
4. 对数据进行预处理（过滤、转换、聚合等）
5. 生成符合规范的 ECharts 图表配置

当用户提出数据可视化需求时，你应该：
1. 首先理解用户的需求和数据
2. 选择合适的图表类型
3. 如需要，对数据进行预处理
4. 生成图表配置

重要：
- 如果工具调用失败，尝试提供清晰的错误信息
- 保持回复简洁、专业
"""


def create_dataviz_agent(cfg: Dict[str, Any]):
    """创建 DataViz Agent（简化版本）。

    Args:
        cfg: LLM 配置字典

    Returns:
        配置好的 LLM 实例，可用于对话
    """
    wrapper = get_llm_wrapper(cfg)
    return wrapper


def run_dataviz_agent(
    cfg: Dict[str, Any],
    user_input: str,
    data: Optional[Dict[str, Any]] = None,
    chat_history: Optional[List] = None,
) -> Dict[str, Any]:
    """运行 DataViz Agent 的便捷函数。

    Args:
        cfg: LLM 配置
        user_input: 用户的输入
        data: 可选的附加数据
        chat_history: 可选的对话历史

    Returns:
        Agent 执行结果
    """
    wrapper = create_dataviz_agent(cfg)

    messages = []
    messages.append({"role": "system", "content": DATAVIZ_SYSTEM_PROMPT})

    if chat_history:
        for msg in chat_history:
            if isinstance(msg, dict):
                messages.append(msg)

    messages.append({"role": "user", "content": user_input})

    try:
        response = wrapper.call_llm(
            messages=messages,
            max_tokens=cfg.get("max_tokens", 2048),
            temperature=cfg.get("temperature", 0.7),
        )
        return {
            "success": True,
            "output": response,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
