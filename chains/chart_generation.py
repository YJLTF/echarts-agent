#!/usr/bin/env python3
"""图表生成的 LCEL Chain。"""
import json
from typing import Any, Dict, Iterator, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda

from llm_client import get_llm_wrapper, compute_column_stats, call_llm_stream
from knowledge import get_knowledge_for_type
from prompts.chart_generation import build_chart_user_prompt
from output_parsers.chart_parser import parse_chart_response


# ECharts 响应 schema
CHART_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "option": {
            "type": "object",
            "description": "ECharts 配置对象"
        },
        "content": {
            "type": "string",
            "description": "图表解读文字（30-80字中文）"
        }
    },
    "required": ["option", "content"]
}


def build_chart_generation_chain(cfg: Dict[str, Any]):
    """构建图表生成的 LCEL 链。

    Args:
        cfg: LLM 配置字典

    Returns:
        一个 Runnable 对象
    """
    wrapper = get_llm_wrapper(cfg)

    def build_prompt(inputs: dict) -> dict:
        """构建完整的用户 prompt。"""
        prompt = inputs.get("prompt", "")
        data = inputs.get("data")
        chart_type = inputs.get("chart_type", "bar")
        style_hint = inputs.get("style_hint")
        knowledge = inputs.get("knowledge")
        preprocess_info = inputs.get("preprocess_info")

        user_prompt = build_chart_user_prompt(
            prompt=prompt,
            data=data,
            chart_type=chart_type,
            style_hint=style_hint,
            knowledge=knowledge,
            preprocess_info=preprocess_info,
        )

        return {
            "user_prompt": user_prompt,
            "system_prompt": cfg.get("system_prompt", ""),
        }

    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_prompt}"),
        ("human", "{user_prompt}"),
    ])

    def invoke_llm(inputs: dict) -> str:
        """调用 LLM。"""
        system = inputs.get("system_prompt", "") or (
            "你是一个专业的 ECharts 图表生成助手，请根据用户需求生成符合规范的 ECharts 配置。"
        )
        user = inputs.get("user_prompt", "")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        return wrapper.call_llm(
            messages=messages,
            max_tokens=cfg.get("max_tokens", 2048),
            temperature=cfg.get("temperature", 0.7),
            response_format=CHART_RESPONSE_SCHEMA,
            reasoning_effort=cfg.get("reasoning_effort"),
        )

    chain = (
        RunnableLambda(build_prompt)
        | prompt
        | RunnableLambda(invoke_llm)
    )

    return chain


def generate_chart(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str = "bar",
    style_hint: dict = None,
    preprocess_info: dict = None,
) -> tuple[Optional[dict], Optional[str], str]:
    """生成图表的便捷函数。

    Returns:
        (option, content, parse_method 或 error_message)
    """
    chain = build_chart_generation_chain(cfg)

    # 获取知识库
    knowledge = get_knowledge_for_type(chart_type)

    input_data = {
        "prompt": prompt,
        "data": data,
        "chart_type": chart_type,
        "style_hint": style_hint,
        "knowledge": knowledge,
        "preprocess_info": preprocess_info,
    }

    try:
        raw = chain.invoke(input_data)
        option, content, parse_method, error = parse_chart_response(raw)
        if error:
            return None, None, error
        return option, content, parse_method
    except Exception as e:
        return None, None, str(e)


def generate_chart_stream(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str = "bar",
    style_hint: dict = None,
    preprocess_info: dict = None,
) -> Iterator[str]:
    """流式生成图表。"""
    wrapper = get_llm_wrapper(cfg)

    # 获取知识库
    knowledge = get_knowledge_for_type(chart_type)

    user_prompt = build_chart_user_prompt(
        prompt=prompt,
        data=data,
        chart_type=chart_type,
        style_hint=style_hint,
        knowledge=knowledge,
        preprocess_info=preprocess_info,
    )

    system_prompt = cfg.get("system_prompt", "") or (
        "你是一个专业的 ECharts 图表生成助手。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    return wrapper.call_llm_stream(
        messages=messages,
        max_tokens=cfg.get("max_tokens", 2048),
        temperature=cfg.get("temperature", 0.7),
        response_format=CHART_RESPONSE_SCHEMA,
        reasoning_effort=cfg.get("reasoning_effort"),
    )
