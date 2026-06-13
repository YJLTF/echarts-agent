#!/usr/bin/env python3
"""数据解析模块：解析 Excel / CSV / JSON / 文本数据。"""
import io
import json
import csv as _csv
from typing import Any, Dict, List

import pandas as pd


ALLOWED_EXT = {"xlsx", "xls", "csv", "json", "txt"}


def _clean(v):
    if isinstance(v, (float, int)):
        if isinstance(v, float) and v != v:  # NaN
            return None
        return v
    return v


def _df_to_dict(df: pd.DataFrame) -> Dict[str, Any]:
    df = df.copy()
    # 尝试解析日期列
    for col in df.columns:
        try:
            s = pd.to_datetime(df[col], errors="ignore")
            if pd.api.types.is_datetime64_any_dtype(s.dtype):
                df[col] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    columns = [str(c) for c in df.columns]
    rows = []
    for _, row in df.iterrows():
        rows.append({c: _clean(row[c]) for c in columns})
    return {"columns": columns, "rows": rows, "count": len(rows)}


def parse_upload(file_storage) -> Dict[str, Any]:
    filename = (file_storage.filename or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    raw = file_storage.read()
    if ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(raw))
        data = _df_to_dict(df)
    elif ext == "csv":
        text = _decode_text(raw)
        df = pd.read_csv(io.StringIO(text))
        data = _df_to_dict(df)
    elif ext == "json":
        obj = json.loads(_decode_text(raw))
        if isinstance(obj, dict) and list(obj.keys()) and isinstance(list(obj.values())[0], list):
            data = {"columns": list(obj.keys()), "rows": [dict(zip(obj.keys(), vals)) for vals in zip(*obj.values())]}
        elif isinstance(obj, list):
            if obj and isinstance(obj[0], dict):
                columns = sorted({k for r in obj for k in r.keys()})
                data = {"columns": columns, "rows": obj}
            else:
                data = {"columns": ["value"], "rows": [{"value": v} for v in obj]}
        else:
            data = {"columns": ["value"], "rows": [{"value": json.dumps(obj, ensure_ascii=False)}]}
        data["count"] = len(data["rows"])
    else:
        text = _decode_text(raw)
        data = parse_text(text)
    data["source"] = f"file:{filename}"
    data["description"] = describe(data)
    return data


def parse_data_text(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                columns = sorted({k for r in obj for k in r.keys()})
                data = {"columns": columns, "rows": obj, "count": len(obj)}
            elif isinstance(obj, list):
                data = {"columns": ["value"], "rows": [{"value": v} for v in obj], "count": len(obj)}
            elif isinstance(obj, dict) and list(obj.keys()) and isinstance(list(obj.values())[0], list):
                data = {"columns": list(obj.keys()), "rows": [dict(zip(obj.keys(), vals)) for vals in zip(*obj.values())]}
                data["count"] = len(data["rows"])
            else:
                data = {"columns": ["value"], "rows": [{"value": json.dumps(obj, ensure_ascii=False)}], "count": 1}
            data["source"] = "text:json"
            data["description"] = describe(data)
            return data
        except Exception:
            pass
    data = parse_text(text)
    data["source"] = "text"
    data["description"] = describe(data)
    return data


def parse_text(text: str) -> Dict[str, Any]:
    """作为最后兜底：尝试按 CSV/TSV/空格分隔解析表格。"""
    # 优先识别多行为一张表
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"columns": [], "rows": [], "count": 0}
    # 若只有一行，作为 key=value 或 JSON？再退化为单列
    if len(lines) == 1:
        data = {"columns": ["text"], "rows": [{"text": lines[0]}], "count": 1}
        return data
    # CSV 嗅探
    try:
        dialect = _csv.Sniffer().sniff("\n".join(lines[:5]), delimiters=",;\t|")
        reader = _csv.reader(lines, dialect)
        rows = list(reader)
        if rows:
            cols = [c.strip() for c in rows[0]]
            data_rows = []
            for r in rows[1:]:
                d = {}
                for i, c in enumerate(cols):
                    d[c] = r[i] if i < len(r) else None
                data_rows.append(d)
            return {"columns": cols, "rows": data_rows, "count": len(data_rows)}
    except Exception:
        pass
    # 按空格分隔
    first = lines[0].split()
    cols = [f"col{i+1}" for i in range(len(first))]
    data_rows = []
    for ln in lines:
        parts = ln.split()
        d = {f"col{i+1}": (parts[i] if i < len(parts) else None for i in range(len(first)))}
        data_rows.append(d)
    return {"columns": cols, "rows": data_rows, "count": len(data_rows)}


def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def describe(data: Dict[str, Any]) -> str:
    cols = data.get("columns", [])
    rows = data.get("rows", [])
    return (
        f"共 {len(rows)} 行 × {len(cols)} 列；"
        f"列名：{', '.join(cols)}。"
    )
