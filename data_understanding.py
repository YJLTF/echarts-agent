#!/usr/bin/env python3
"""LLM 数据理解与整理模块。

设计目标：
- 用户的真实数据往往不规范（多级表头、含单位/百分号/千分位、合并单元格、
  转置表、噪声行/合计行/重复表头等），单纯靠 pandas/csv 解析常常失败或
  得到脏的列名、错乱的类型、错误的结构。
- 本模块基于「代码解析草稿」+「原始文本片段」，调用大模型去理解和整理数据，
  输出统一的规范表格描述：columns(name/type/role/description) + rows + 备注。
- 校验 LLM 返回：优先用 LangChain 的 ``with_structured_output(DataUnderstandingResponse)``
  （自动注入 JSON schema 说明 + 失败重试）；structured 不支持时降级到 ``JsonOutputParser``；
  还不行才回到原始 parsed，保证前端可用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser

from data_parser import _decode_text
from llm_client import ChatOpenAIWrapper
from output_parsers.schema import (
    ColumnSchema,
    DataUnderstandingResponse,
)
from prompts import data_understanding_prompt_template


MAX_RAW_CHARS = 6000      # 喂给 LLM 的原始文本上限
MAX_SAMPLE_ROWS = 8       # 草稿样例行数
MAX_LLM_ROWS = 30         # 期望 LLM 输出的最大行数
MAX_TOKENS = 2048

_VALID_TYPES = {"string", "number", "date", "boolean"}
_VALID_ROLES = {"category", "value", "time", "series", "label", "ignore"}


# ========================================================================
# Prompt 模板（从 prompts 模块拿）
# ========================================================================

def _build_messages(hint: str, cols: str, count: int, sample_n: int,
                    sample: str, stats: str, raw_chars: int, raw: str) -> List[Dict[str, str]]:
    """构造对话消息列表（system + user），从 :func:`prompts.data_understanding_prompt_template` 渲染。"""
    rendered = data_understanding_prompt_template().format_messages(
        hint=hint, cols=cols, count=count, sample_n=sample_n,
        sample=sample, stats=stats, raw_chars=raw_chars, raw=raw,
    )
    out: List[Dict[str, str]] = []
    for m in rendered:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": m.content})
    return out


# ========================================================================
# JSON 抽取与校验（降级路径用 —— structured / JsonOutputParser 失败时）
# ========================================================================

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_MAX_JSON_CANDIDATE = 500_000


def _scan_json_object(raw: str, max_size: int = _MAX_JSON_CANDIDATE) -> Optional[str]:
    """栈式扫描 raw 中第一个完整顶层 JSON 对象。"""
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    n = len(raw)
    for i in range(start, n):
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
                end = i + 1
                if end - start > max_size:
                    return None
                return raw[start:end]
    return None


def _extract_json(raw: str) -> Optional[dict]:
    """从 LLM 文本中抽取 JSON 对象（降级路径用）。"""
    if not raw:
        return None
    m = _JSON_FENCE.search(raw)
    candidate: Optional[str] = m.group(1) if m else None
    if not candidate:
        candidate = _scan_json_object(raw)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except Exception:
        cleaned = _TRAILING_COMMA.sub(r"\1", candidate)
        try:
            obj = json.loads(cleaned)
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


def _to_number(s: str) -> Optional[float]:
    """把带单位/百分号的字符串转成 float。"""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    is_pct = "%" in s
    cleaned = (
        s.replace(",", "").replace(" ", "")
        .replace("万", "").replace("亿", "")
        .replace("元", "").replace("¥", "").replace("$", "")
        .replace("%", "")
    )
    try:
        v = float(cleaned)
        return v / 100.0 if is_pct else v
    except Exception:
        return None


def _coerce_value(v: Any) -> Any:
    """尽量把字符串值转成 number/boolean。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "":
        return ""
    low = s.lower()
    if low in ("true", "yes", "是", "y"):
        return True
    if low in ("false", "no", "否", "n"):
        return False
    return s


def _normalize_dict(obj: dict) -> Tuple[Optional[dict], Optional[str]]:
    """校验并归一化 LLM 返回的 dict 结构（降级路径用）。"""
    cols = obj.get("columns")
    rows = obj.get("rows")
    if not isinstance(cols, list) or not cols:
        return None, "缺少 columns"
    if not isinstance(rows, list):
        return None, "rows 不是数组"

    clean_cols: List[Dict[str, str]] = []
    seen_names: Dict[str, int] = {}
    for c in cols:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        if name in seen_names:
            seen_names[name] += 1
            name = f"{name}_{seen_names[name]}"
        else:
            seen_names[name] = 1
        ctype = str(c.get("type") or "string").strip().lower()
        if ctype not in _VALID_TYPES:
            ctype = "string"
        role = str(c.get("role") or "value").strip().lower()
        if role not in _VALID_ROLES:
            role = "value"
        clean_cols.append({
            "name": name, "type": ctype, "role": role,
            "description": str(c.get("description") or "").strip(),
        })
    if not clean_cols:
        return None, "columns 为空"

    return ({
        "columns": clean_cols,
        "rows": _normalize_rows(rows, clean_cols),
        "summary": str(obj.get("summary") or "").strip(),
        "notes": str(obj.get("notes") or "").strip(),
    }, None)


def _normalize_rows(
    rows: List[Any],
    cols: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """把 LLM 返回的 rows 列表归一化：按列类型做 coerce / 类型转换 / 缺失列填空字符串。

    共用函数 — 用于降级路径的 ``_normalize_dict`` 和 Pydantic 路径的 ``_pydantic_to_dict``。
    """
    valid_names = {c["name"] for c in cols}
    type_by_name = {c["name"]: c["type"] for c in cols}
    clean_rows: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out: Dict[str, Any] = {}
        for k, v in r.items():
            key = str(k).strip()
            if key not in valid_names:
                continue
            value = _coerce_value(v)
            t = type_by_name[key]
            if t == "number" and isinstance(value, str):
                num = _to_number(value)
                if num is not None:
                    value = num
            elif t == "boolean" and isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "yes", "是", "y", "1"):
                    value = True
                elif low in ("false", "no", "否", "n", "0"):
                    value = False
            out[key] = value
        for n in valid_names:
            if n not in out:
                out[n] = ""
        clean_rows.append(out)
    return clean_rows


def _pydantic_to_dict(result: DataUnderstandingResponse) -> dict:
    """``DataUnderstandingResponse`` Pydantic 对象 → 内部 dict 形式。"""
    cols = [
        {
            "name": c.name,
            "type": c.type if c.type in _VALID_TYPES else "string",
            "role": c.role if c.role in _VALID_ROLES else "value",
            "description": c.description or "",
            "unit": getattr(c, "unit", None) or "",
        }
        for c in result.columns
    ]
    return {
        "columns": cols,
        "rows": _normalize_rows(result.rows or [], cols),
        "summary": result.summary or "",
        "notes": result.notes or "",
    }


# ========================================================================
# 公开入口
# ========================================================================

def build_raw_preview(
    filename: str,
    ext: str,
    parsed: Dict[str, Any],
    raw_bytes: Optional[bytes] = None,
) -> str:
    """为 LLM 准备一段「原始数据片段」。优先用 raw_bytes，否则用代码解析草稿重建。"""
    if raw_bytes is not None:
        text = _decode_text(raw_bytes)
        if text:
            return text[:MAX_RAW_CHARS]

    rows = parsed.get("rows") or []
    cols = parsed.get("columns") or []
    if not rows or not cols:
        return ""
    sample = rows[: min(15, len(rows))]
    lines = [",".join([str(c) for c in cols])]
    for r in sample:
        lines.append(",".join([str(r.get(c, "")) for c in cols]))
    return ("\n".join(lines))[:MAX_RAW_CHARS]


def _llm_understand(wrapper: ChatOpenAIWrapper, messages: List[Dict[str, str]]) -> Tuple[Optional[DataUnderstandingResponse], str, Optional[str]]:
    """三层调用策略：

    1) ``wrapper.structured(DataUnderstandingResponse).invoke(messages)`` —— 严格 schema + 重试
    2) ``JsonOutputParser().invoke(wrapper.call_llm_stream(...))`` —— 自由 JSON，宽容解析
    3) 返回 ``(None, raw_reply, None)`` 让上层决定是否再走降级路径
    """
    # 路径 1：with_structured_output
    try:
        structured = wrapper.structured(
            DataUnderstandingResponse,
            max_tokens=MAX_TOKENS,
            temperature=0.2,
        )
        result = structured.invoke(messages)
        if isinstance(result, DataUnderstandingResponse):
            return result, "", None
    except Exception:
        pass  # 走路径 2/3 兜底

    # 路径 2：JsonOutputParser 降级
    raw_reply = ""
    try:
        for chunk in wrapper.call_llm_stream(
            messages,
            max_tokens=MAX_TOKENS,
            temperature=0.2,
        ):
            raw_reply += chunk
    except Exception as e:
        return None, "", f"LLM 调用失败：{e}"

    try:
        obj = JsonOutputParser().invoke(raw_reply)
    except Exception as e:
        return None, raw_reply, f"JsonOutputParser 解析失败：{e}"

    if not isinstance(obj, dict):
        return None, raw_reply, "LLM 返回不是 JSON 对象"

    try:
        result = DataUnderstandingResponse.model_validate(obj)
        return result, raw_reply, None
    except Exception as e:
        return None, raw_reply, f"Pydantic 校验失败：{e}"


def understand_data(
    cfg: Dict[str, Any],
    raw_preview: str,
    parsed: Dict[str, Any],
    user_hint: str = "",
) -> Dict[str, Any]:
    """调用 LLM 把不规范数据整理成规范表格。

    返回与 data_parser 输出兼容的结构：

    ::
        {
          "columns": [{"name","type","role","description"} | str, ...],
          "rows": [...], "count": int, "summary": str, "notes": str,
          "description": str, "column_names": [...],
          "understand_method": "llm|fallback",
          "understand_error": str | None,
        }
    """
    base_columns = parsed.get("columns") or []
    base_rows = parsed.get("rows") or []
    if not base_columns and not raw_preview:
        return {
            "columns": [], "rows": [], "count": 0,
            "summary": "", "notes": "没有可用数据。",
            "understand_method": "fallback",
            "understand_error": "empty input",
        }

    cols_line = ", ".join(str(c) for c in base_columns) or "(无)"
    sample_rows = base_rows[:MAX_SAMPLE_ROWS]
    sample_text = json.dumps(sample_rows, ensure_ascii=False, indent=2) if sample_rows else "(无)"
    from llm_client import compute_column_stats
    col_names = [str(c) for c in base_columns]
    stats_text = compute_column_stats(base_rows, col_names) or "（样本太小，无需统计）"

    messages = _build_messages(
        hint=(user_hint or "（无）").strip() or "（无）",
        cols=cols_line,
        count=len(base_rows),
        sample_n=len(sample_rows),
        sample=sample_text,
        stats=stats_text,
        raw_chars=MAX_RAW_CHARS,
        raw=(raw_preview or "").strip()[:MAX_RAW_CHARS] or "(无)",
    )

    wrapper = _wrapper_from_cfg(cfg)
    result, raw_reply, err = _llm_understand(wrapper, messages)
    if result is None:
        # 路径 3：再降级 —— 手动 _extract_json + _normalize_dict（最坏情况兜底）
        if raw_reply:
            obj = _extract_json(raw_reply)
            if obj is not None:
                cleaned, norm_err = _normalize_dict(obj)
                if cleaned is not None:
                    return _ok_response(cleaned, "llm", None, raw_reply)
                err = f"normalize 失败：{norm_err}"
        return _fallback(parsed, err or "unknown")

    cleaned = _pydantic_to_dict(result)
    return _ok_response(cleaned, "llm", None, raw_reply)


def _wrapper_from_cfg(cfg: Dict[str, Any]) -> ChatOpenAIWrapper:
    """从 cfg dict 拿 wrapper（懒加载 + 缓存）。"""
    from llm_client import get_llm_wrapper
    return get_llm_wrapper(cfg)


def _ok_response(cleaned: dict, method: str, err: Optional[str], raw_reply: str) -> Dict[str, Any]:
    """LLM 成功时构造对外返回结构。"""
    flat_columns = [c["name"] for c in cleaned["columns"]]
    description = (
        f"LLM 整理后：{len(cleaned['rows'])} 行 × {len(flat_columns)} 列；"
        f"列名：{', '.join(flat_columns)}。"
        + (f" 说明：{cleaned['summary']}" if cleaned.get("summary") else "")
    )
    return {
        "columns": cleaned["columns"],
        "column_names": flat_columns,
        "rows": cleaned["rows"],
        "count": len(cleaned["rows"]),
        "summary": cleaned.get("summary", ""),
        "notes": cleaned.get("notes", ""),
        "description": description,
        "understand_method": method,
        "understand_error": err,
        "raw_reply": raw_reply,
    }


def _fallback(parsed: Dict[str, Any], err: str) -> Dict[str, Any]:
    """LLM 解析失败时回退到代码解析结果，并附加标注。"""
    result = {**parsed, "understand_method": "fallback", "understand_error": err}
    cols = parsed.get("columns") or []
    if cols and isinstance(cols[0], str):
        result["column_names"] = cols
        result["columns"] = [
            {"name": c, "type": "string", "role": "value", "description": ""}
            for c in cols
        ]
    return result
