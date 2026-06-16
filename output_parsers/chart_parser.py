#!/usr/bin/env python3
"""图表生成的结构化输出解析器。"""
import json
import re
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field


class ChartResponse(BaseModel):
    """图表生成的响应结构。"""
    option: Dict[str, Any] = Field(description="ECharts 配置对象")
    content: str = Field(description="图表解读文字（30-80字中文）")


class DataUnderstandingResponse(BaseModel):
    """数据理解与整理的响应结构。"""
    columns: list = Field(description="列定义列表")
    rows: list = Field(description="数据行列表")
    summary: str = Field(default="", description="数据集摘要")
    notes: str = Field(default="", description="整理说明")


def parse_chart_response(raw: str) -> Tuple[Optional[Dict], Optional[str], str, str]:
    """解析图表生成的原始响应。

    Returns:
        (option, content, parse_method, error_message)
    """
    # 去除 <think>...</think> 块
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

    def _validate(obj) -> Tuple[Optional[dict], Optional[str]]:
        if not isinstance(obj, dict):
            return None, None
        option = obj.get("option")
        content = obj.get("content")
        if isinstance(option, dict) and isinstance(content, str):
            return option, content
        return None, None

    def _is_echarts_option_shape(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        return any(k in obj for k in ("series", "title", "xAxis", "yAxis", "legend", "tooltip"))

    def _extract_option_and_content(obj: dict) -> Tuple[Optional[dict], Optional[str], bool]:
        if not _is_echarts_option_shape(obj):
            return obj, "", False
        content_val = obj.get("content")
        if isinstance(content_val, str) and content_val.strip():
            new_option = {k: v for k, v in obj.items() if k != "content"}
            return new_option, content_val, True
        return obj, "", False

    def _strip_json_fence(raw: str) -> Tuple[str, str]:
        """去掉 ```json ... ``` 围栏，返回 (围栏内, 围栏外)。"""
        m = re.match(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.IGNORECASE)
        if m:
            inside = m.group(1)
            outside = raw[m.end():].strip()
            return inside, outside
        return "", raw

    # 1) 主路径：直接 json.loads
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None

    if obj is not None:
        got = _validate(obj)
        if got[0] is not None and got[1] is not None:
            return got[0], got[1], "primary", ""
        if _is_echarts_option_shape(obj):
            opt2, cnt2, popped = _extract_option_and_content(obj)
            return opt2, cnt2, ("in_option" if popped else "primary_bare"), ""

    # 2) 兜底：strip ```json...``` 围栏
    inside, outside = _strip_json_fence(raw)
    if inside:
        try:
            inner_obj = json.loads(inside)
        except Exception:
            inner_obj = None
        if isinstance(inner_obj, dict):
            got = _validate(inner_obj)
            if got[0] is not None and got[1] is not None:
                return got[0], got[1], "fence_full", ""
            if _is_echarts_option_shape(inner_obj):
                opt2, cnt2, popped = _extract_option_and_content(inner_obj)
                if outside.strip():
                    return opt2, outside, "fence_option", ""
                return opt2, cnt2, ("fence_in_option" if popped else "fence_option"), ""

    preview = raw.strip().replace("\n", " ")[:160]
    return None, None, None, f"模型输出不符合结构化 schema：JSON.loads 失败或字段缺失（reply: {preview!r}）"


def parse_data_understanding_response(raw: str) -> Tuple[Optional[dict], Optional[str]]:
    """解析数据理解与整理的原始响应。

    Returns:
        (parsed_data, error_message)
    """
    import re

    _JSON_FENCE = re.compile(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", re.IGNORECASE)
    _TRAILING_COMMA = re.compile(r",\s*([}\]])")

    if not raw:
        return None, "空响应"

    # 尝试从围栏中提取
    m = _JSON_FENCE.search(raw)
    candidate = m.group(1) if m else None

    if not candidate:
        # 尝试栈式扫描第一个完整 JSON 对象
        start = raw.find("{")
        if start < 0:
            return None, "未找到 JSON 对象"
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:i+1]
                    break

    if not candidate:
        return None, "无法解析 JSON 对象"

    try:
        obj = json.loads(candidate)
    except Exception:
        # 尝试去掉尾随逗号
        cleaned = _TRAILING_COMMA.sub(r"\1", candidate)
        try:
            obj = json.loads(cleaned)
        except Exception as e:
            return None, f"JSON 解析失败：{e}"

    if not isinstance(obj, dict):
        return None, "响应不是 JSON 对象"

    # 验证必要字段
    if "columns" not in obj and "rows" not in obj:
        return None, "缺少 columns 或 rows 字段"

    return obj, None
