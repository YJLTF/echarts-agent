#!/usr/bin/env python3
"""LLM 数据理解与整理模块。

设计目标：
- 用户的真实数据往往不规范（多级表头、含单位/百分号/千分位、合并单元格、
  转置表、噪声行/合计行/重复表头等），单纯靠 pandas/csv 解析常常失败或
  得到脏的列名、错乱的类型、错误的结构。
- 本模块基于「代码解析草稿」+「原始文本片段」，调用大模型去理解和整理数据，
  输出统一的规范表格描述：columns(name/type/role/description) + rows + 备注。
- 校验 LLM 返回：抽取 JSON、验证 schema、修正非法结构；校验失败时安全
  fallback 回代码解析结果，保证前端可用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from llm_client import call_llm
from data_parser import _decode_text


SYSTEM_PROMPT = """你是一名资深的数据整理助手，擅长把不规范的真实数据整理成规范的表格。

用户上传的数据可能存在以下问题，代码解析常常只能得到粗略甚至错误的草稿：
- 多级表头、合并单元格、中英文混排
- 数值单元格带单位（"万"、"%"、"元"）、带千分位（"1,234"）
- 合计行、空行、表头重复行
- 转置表、行被错位、列缺失
- 字段命名含前后空格、不可见字符、同义不同名

请你结合「原始文本片段」与「代码解析草稿」完成整理工作，严格按下面的 JSON Schema 输出，
**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀：

{
  "columns": [
    {
      "name": "规范化后的列名（中文保持中文，英文保持英文；去空格/单位）",
      "type": "string | number | date | boolean 之一",
      "role": "category | value | time | series | label | ignore 之一",
      "description": "该列含义的一行中文说明"
    }
  ],
  "rows": [
    {"规范化列名1": 值, "规范化列名2": 值, ...}
  ],
  "summary": "一句话中文总结这个数据集的内容",
  "notes": "做了哪些清洗动作（如去单位/剔合计行/合并表头等）；若代码草稿无误可写 '无'"
}

约束：
1) rows 中所有 key 必须与 columns 中的 name 完全一致。
2) number 类型必须输出为 JSON number（不要带引号、不要带单位）；date 输出 "YYYY-MM-DD" 字符串；
   boolean 输出 true/false；其它一律 string。
3) 行数应与原始数据有效行一致（不要人为缩减，也不要造数据）。如果原始行数很多，
   至少给出前 30 行作为示例，并完整保留所有列结构。
4) 严格过滤：合计行/总计行/空行/重复表头一律剔除。
5) 若某列无意义或纯粹是噪声，role 填 "ignore"，但仍保留在 columns 里、rows 中可填空字符串。
6) 不要在 JSON 之外输出任何字符。
"""


USER_PROMPT_TEMPLATE = """【用户意图（可选）】
{hint}

【代码解析草稿】
列名：{cols}
共 {count} 行；前 {sample_n} 行样例：
{sample}

【列统计摘要（>20 行才生成）】
{stats}

【原始数据片段（最多 {raw_chars} 字符）】
{raw}

请输出整理后的 JSON。
"""


MAX_RAW_CHARS = 6000      # 喂给 LLM 的原始文本上限
MAX_SAMPLE_ROWS = 8       # 草稿样例行数
MAX_LLM_ROWS = 30         # 期望 LLM 输出的最大行数
MAX_TOKENS = 2048


# ----------------------------- JSON 抽取与校验 -----------------------------
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
# 候选 JSON 体积上限：超过即放弃（防 LLM 异常输出巨大字符串时拖累 json.loads）。
_MAX_JSON_CANDIDATE = 500_000


def _scan_json_object(raw: str, max_size: int = _MAX_JSON_CANDIDATE) -> Optional[str]:
    """栈式扫描 raw 中**第一个完整顶层 JSON 对象**。

    比 ``raw.find("{") + raw.rfind("}")`` 切片更窄 —— 不会把后续解释/噪声一起包进来。
    候选超过 ``max_size`` 直接返回 ``None``。
    """
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
    """从 LLM 文本中抽取 JSON 对象。"""
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
        # 尝试去掉尾随逗号等常见错误
        cleaned = _TRAILING_COMMA.sub(r"\1", candidate)
        try:
            obj = json.loads(cleaned)
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


_VALID_TYPES = {"string", "number", "date", "boolean"}
_VALID_ROLES = {"category", "value", "time", "series", "label", "ignore"}


def _coerce_value(v: Any) -> Any:
    """尽量把字符串值转成 number/boolean，提升数据一致性。"""
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
    # 去千分位
    cleaned = s.replace(",", "").replace(" ", "")
    # 数字（含百分号/单位）—— 只在调用方明确 type=number 时才转换；这里保持原样
    return s


def _validate(obj: dict) -> Tuple[Optional[dict], Optional[str]]:
    """校验并归一化 LLM 返回的 JSON 结构。"""
    if not isinstance(obj, dict):
        return None, "LLM 返回不是 JSON 对象"

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
        # 重名处理
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
        clean_cols.append(
            {
                "name": name,
                "type": ctype,
                "role": role,
                "description": str(c.get("description") or "").strip(),
            }
        )
    if not clean_cols:
        return None, "columns 为空"

    valid_names = {c["name"] for c in clean_cols}
    type_by_name = {c["name"]: c["type"] for c in clean_cols}
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
            # 按声明的类型再做一次轻量转换
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
        # 缺失列填空字符串
        for n in valid_names:
            if n not in out:
                out[n] = ""
        clean_rows.append(out)

    return (
        {
            "columns": clean_cols,
            "rows": clean_rows,
            "summary": str(obj.get("summary") or "").strip(),
            "notes": str(obj.get("notes") or "").strip(),
        },
        None,
    )


def _to_number(s: str) -> Optional[float]:
    """把带单位/百分号的字符串转成 float。"""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    is_pct = "%" in s
    cleaned = (
        s.replace(",", "")
        .replace(" ", "")
        .replace("万", "")
        .replace("亿", "")
        .replace("元", "")
        .replace("¥", "")
        .replace("$", "")
        .replace("%", "")
    )
    try:
        v = float(cleaned)
        return v / 100.0 if is_pct else v
    except Exception:
        return None


# ----------------------------- 公开入口 -----------------------------
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


def understand_data(
    cfg: Dict[str, Any],
    raw_preview: str,
    parsed: Dict[str, Any],
    user_hint: str = "",
) -> Dict[str, Any]:
    """调用 LLM 把不规范数据整理成规范表格。

    返回与 data_parser 输出兼容的结构：
        {
          "columns": [{"name","type","role","description"} | str, ...],
          "rows": [...],
          "count": int,
          "summary": str,
          "notes": str,
          "understand_method": "llm|fallback",
          "understand_error": str | None,
        }
    当 LLM 调用或校验失败时，自动回退到原始 parsed 并标记 understand_method=fallback。
    """
    base_columns = parsed.get("columns") or []
    base_rows = parsed.get("rows") or []
    if not base_columns and not raw_preview:
        return {
            "columns": [],
            "rows": [],
            "count": 0,
            "summary": "",
            "notes": "没有可用数据。",
            "understand_method": "fallback",
            "understand_error": "empty input",
        }

    cols_line = ", ".join(str(c) for c in base_columns) or "(无)"
    sample_rows = base_rows[:MAX_SAMPLE_ROWS]
    sample_text = json.dumps(sample_rows, ensure_ascii=False, indent=2) if sample_rows else "(无)"
    # 列统计：和 chart prompt 共享同一份「全量画像」；<20 行不发
    from llm_client import compute_column_stats
    col_names = [str(c) for c in base_columns]
    stats_text = compute_column_stats(base_rows, col_names) or "（样本太小，无需统计）"
    user_prompt = USER_PROMPT_TEMPLATE.format(
        hint=(user_hint or "（无）").strip() or "（无）",
        cols=cols_line,
        count=len(base_rows),
        sample_n=len(sample_rows),
        sample=sample_text,
        stats=stats_text,
        raw_chars=MAX_RAW_CHARS,
        raw=(raw_preview or "").strip()[:MAX_RAW_CHARS] or "(无)",
    )

    raw_reply = ""
    try:
        raw_reply = call_llm(
            cfg,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.2,
        )
    except Exception as e:
        return _fallback(parsed, f"LLM 调用失败：{e}")

    obj = _extract_json(raw_reply)
    if obj is None:
        preview = (raw_reply or "").strip().replace("\n", " ")[:160]
        return _fallback(parsed, f"无法从 LLM 回复中解析 JSON（reply: {preview!r}）")
    cleaned, err = _validate(obj)
    if err or not cleaned:
        return _fallback(parsed, f"LLM 返回结构不合法：{err or 'unknown'}")

    columns_norm = cleaned["columns"]
    rows_norm = cleaned["rows"]

    # 统一对外列名形式：同时给出「带 schema 的 columns」和「平铺列名列表」
    flat_columns = [c["name"] for c in columns_norm]
    description = (
        f"LLM 整理后：{len(rows_norm)} 行 × {len(flat_columns)} 列；"
        f"列名：{', '.join(flat_columns)}。"
        + (f" 说明：{cleaned['summary']}" if cleaned.get("summary") else "")
    )

    return {
        "columns": columns_norm,        # 完整 schema
        "column_names": flat_columns,   # 兼容旧字段
        "rows": rows_norm,
        "count": len(rows_norm),
        "summary": cleaned.get("summary", ""),
        "notes": cleaned.get("notes", ""),
        "description": description,
        "understand_method": "llm",
        "understand_error": None,
        "raw_reply": raw_reply,
    }


def _fallback(parsed: Dict[str, Any], err: str) -> Dict[str, Any]:
    """LLM 解析失败时回退到代码解析结果，并附加标注。"""
    result = {
        **parsed,
        "understand_method": "fallback",
        "understand_error": err,
    }
    # 兼容字段
    cols = parsed.get("columns") or []
    if cols and isinstance(cols[0], str):
        result["column_names"] = cols
        result["columns"] = [
            {"name": c, "type": "string", "role": "value", "description": ""}
            for c in cols
        ]
    return result
