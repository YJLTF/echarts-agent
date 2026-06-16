#!/usr/bin/env python3
"""完整的图表生成 Pipeline Chain。"""
import json
from typing import Any, Dict, Iterator, Optional

from llm_client import get_llm_wrapper, pick_chart_type, compute_column_stats
from knowledge import get_knowledge_for_type
from data_preprocessing import preprocess_data
from prompts.chart_generation import build_chart_user_prompt
from output_parsers.chart_parser import parse_chart_response
from .chart_generation import CHART_RESPONSE_SCHEMA, generate_chart_stream


def run_chart_pipeline(
    cfg: dict,
    prompt: str,
    data: Any,
    chart_type_hint: str,
    style_hint: Any,
    *,
    stream: bool = False,
):
    """统一的图表生成 pipeline，按阶段产出事件。

    Yields:
        dict: 事件对象，``type`` 字段取值：
            - ``stage``：阶段状态变更
            - ``delta``：模型流式输出
            - ``done``：最终结果
            - ``error``：错误
    """
    # ---- 1) 数据准备（瞬时） ----
    yield {"type": "stage", "stage": "prepare", "label": "数据准备", "status": "start"}
    if not prompt and not data:
        yield {"type": "error", "message": "请至少输入需求或提供数据。", "status": 400}
        return
    yield {"type": "stage", "stage": "prepare", "status": "done"}

    # ---- 2) 智能数据整理 ----
    yield {"type": "stage", "stage": "understand", "label": "智能数据整理", "status": "start"}
    understanding = None
    existing_method = (data or {}).get("understand_method")
    need_understanding = bool(data and (data.get("need_understanding")))

    if existing_method in ("llm", "fallback"):
        understanding = {
            "method": existing_method,
            "summary": (data or {}).get("summary", "") or "",
            "notes": (data or {}).get("notes", "") or "",
            "error": (data or {}).get("understand_error"),
            "reused": True,
        }
        yield {"type": "stage", "stage": "understand", "status": "done", "understanding": understanding}
    elif need_understanding:
        try:
            from .understanding import invoke_data_understanding
            result = invoke_data_understanding(
                cfg,
                data,
                user_hint=prompt,
                raw_preview=(data or {}).get("raw_text", ""),
            )
            data = result
            understanding = {
                "method": result.get("understand_method"),
                "summary": result.get("summary", ""),
                "notes": result.get("notes", ""),
                "error": result.get("understand_error"),
            }
        except Exception as e:
            understanding = {"method": "fallback", "summary": "", "notes": "", "error": str(e)}
        yield {"type": "stage", "stage": "understand", "status": "done", "understanding": understanding}
    else:
        understanding = {"method": "skipped", "summary": "", "notes": "", "error": None}
        yield {"type": "stage", "stage": "understand", "status": "skipped", "understanding": understanding}

    # ---- 3) 数据预处理 ----
    yield {"type": "stage", "stage": "preprocess", "label": "数据预处理", "status": "start"}
    preprocess_info = None
    if data and data.get("rows"):
        try:
            new_data, pp_info = preprocess_data(prompt or "", data)
            preprocess_info = pp_info
            if pp_info.get("rules"):
                data = new_data
        except Exception as e:
            preprocess_info = {"rules": [], "applied": [], "skipped": [], "summary": f"预处理异常：{e}"}
    else:
        preprocess_info = {"rules": [], "applied": [], "skipped": [], "summary": "无可预处理的数据"}

    has_applied = bool(preprocess_info and preprocess_info.get("applied"))
    yield {"type": "stage", "stage": "preprocess", "status": "done" if has_applied else "skipped", "preprocess": preprocess_info}

    # ---- 4) 选择图表类型 ----
    yield {"type": "stage", "stage": "pick_type", "label": "选择图表类型", "status": "start"}
    chosen_type, type_reason = None, None
    try:
        chosen_type, type_reason = pick_chart_type(cfg, prompt, data, chart_type_hint)
    except Exception as e:
        chosen_type, type_reason = "bar", f"LLM 调用失败，默认使用柱状图：{e}"
    if not chosen_type:
        chosen_type, type_reason = "bar", "未能推荐图表类型，默认使用柱状图。"
    yield {"type": "stage", "stage": "pick_type", "status": "done", "chart_type": chosen_type, "reason": type_reason}

    # ---- 5) 检索知识库 + 构造 prompt ----
    kb = get_knowledge_for_type(chosen_type)

    # ---- 6) 主生成：调用 LLM（流式或一次性） ----
    yield {"type": "stage", "stage": "generate", "label": "生成图表配置", "status": "start"}
    raw = ""
    reasoning = ""

    wrapper = get_llm_wrapper(cfg)
    system_prompt = cfg.get("system_prompt", "") or (
        "你是一个专业的 ECharts 图表生成助手。"
    )
    gen_prompt = build_chart_user_prompt(
        prompt=prompt,
        data=data,
        chart_type=chosen_type,
        style_hint=style_hint,
        knowledge=kb,
        preprocess_info=preprocess_info if has_applied else None,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": gen_prompt},
    ]

    try:
        if stream:
            for chunk in generate_chart_stream(cfg, prompt, data, chosen_type, style_hint, preprocess_info if has_applied else None):
                raw += chunk
                if chunk:
                    yield {"type": "delta", "content": chunk}
            reasoning = ""
        else:
            raw, reasoning = wrapper.call_llm_raw(
                messages=messages,
                max_tokens=cfg.get("max_tokens", 2048),
                temperature=cfg.get("temperature", 0.7),
                response_format=CHART_RESPONSE_SCHEMA,
                reasoning_effort=cfg.get("reasoning_effort"),
            )
            if reasoning:
                raw = "<start_reasoning>" + reasoning + "<end_reasoning>\n\n" + raw
    except Exception as e:
        yield {"type": "stage", "stage": "generate", "status": "error", "message": str(e)}
        yield {"type": "error", "message": f"模型调用失败：{e}", "raw_reply": raw, "status": 500}
        return

    yield {"type": "stage", "stage": "generate", "status": "done", "length": len(raw)}

    # ---- 7) 解析 ----
    yield {"type": "stage", "stage": "parse", "label": "解析与校验", "status": "start"}
    option, content, parse_method, parse_error = parse_chart_response(raw)

    if parse_error:
        yield {"type": "stage", "stage": "parse", "status": "error", "message": parse_error}
        yield {"type": "error", "message": f"模型输出不符合结构化 schema：{parse_error}", "raw_reply": raw, "status": 502}
        return

    assert option is not None and content is not None

    yield {"type": "stage", "stage": "parse", "status": "done"}

    resp = {
        "chart_type": chosen_type,
        "type_reason": type_reason,
        "option": option,
        "content": content,
        "raw_reply": raw,
        "parse_method": parse_method or "primary",
    }
    if understanding is not None:
        resp["understanding"] = understanding
    if preprocess_info is not None:
        resp["preprocess"] = preprocess_info

    yield {"type": "done", **resp}
