#!/usr/bin/env python3
"""数据解析模块：解析 Excel / CSV / JSON / 文本数据。"""
import io
import json
import csv as _csv
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


ALLOWED_EXT = {"xlsx", "xls", "csv", "json", "txt"}


def _clean(v):
    # numpy 标量（如 int64 / float64）转成 Python 原生类型，避免 json.dumps 报错
    if hasattr(v, "item") and not isinstance(v, (list, tuple, dict, str, bytes)):
        try:
            v = v.item()
        except (ValueError, TypeError):
            pass
    if isinstance(v, float) and v != v:  # NaN
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return v


def _df_to_dict(df: pd.DataFrame, infer_dates: bool = False) -> Dict[str, Any]:
    df = df.copy()
    # 关键修复：先把列名统一成字符串再做任何后续操作。
    # pandas 读 Excel 时若表头单元格里是数字，会把列名存成 int/float；
    # 此后 iterrows 用 str(列名) 去 row[] 查找会因底层仍是数字 key 而 KeyError。
    df.columns = [str(c) for c in df.columns]
    # 仅在显式开启 + 列长得像日期时才尝试（pd.to_datetime 对全文本列会跑得很慢并产生 UserWarning）
    if infer_dates:
        for col in df.columns:
            if _looks_like_date_column(df[col]):
                try:
                    s = pd.to_datetime(df[col], errors="coerce")
                    if s.notna().sum() >= max(1, len(s) // 2):
                        df[col] = s.dt.strftime("%Y-%m-%d %H:%M:%S").where(s.notna(), df[col])
                except Exception:
                    pass
    columns = list(df.columns)
    rows = []
    for _, row in df.iterrows():
        rows.append({c: _clean(row[c]) for c in columns})
    return {"columns": columns, "rows": rows, "count": len(rows)}


def _looks_like_date_column(s: pd.Series) -> bool:
    """判断一列是否像日期列：用 dateutil 试解析前若干非空值。

    支持的常见写法：
    - ISO: 2024-01-15、2024-01-15T08:00:00
    - 斜线: 2024/01/15、01/15/2024
    - 点: 2024.01.15、15.01.2024
    - 中文: 2024年1月15日、1月15日
    - 纯时间: 08:00:00、08:00
    - 中文时间词: 今天、昨天、前天
    - 英文月: Jan 2024

    如果 ≥ 60% 的采样值能解析为日期，就认定为日期列。
    """
    try:
        from dateutil import parser as _dt
    except Exception:
        return False  # 没装 dateutil 就放弃，不要乱猜

    sample = s.dropna().astype(str).head(8)
    if len(sample) < 2:
        return False

    def _normalize_cn_date(v: str) -> str:
        """2024年1月15日 / 1月15日 → 2024-1-15 / 1-15（dateutil 才能解析）。"""
        # 全角 → 半角
        v = v.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        # "1月15日" → "1-15"（无年）
        v = v.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").strip("-")
        return v

    def _is_pure_number(v: str) -> bool:
        s = v.strip()
        return s.isdigit() or (s.startswith("-") and s[1:].isdigit()) or s.replace(".", "", 1).isdigit()

    hits = 0
    for v in sample:
        v = v.strip()
        if not v:
            continue
        if v in ("今天", "昨天", "前天", "now", "today", "yesterday"):
            hits += 1
            continue
        # 纯数字不是日期（100 会被 dateutil 错认成公元 100 年）
        if _is_pure_number(v):
            continue
        # 中文日期先归一化
        norm = _normalize_cn_date(v) if any(c in v for c in "年月日") else v
        try:
            # dayfirst=False 优先 YYYY-MM-DD；"15/01/2024" 会按 MM/DD/YYYY 解析，
            # 在中文场景里"15/01/2024"更可能是 DD/MM，但 dateutil 默认是 MM/DD/YYYY；
            # 这里我们先按默认试一次（接受 YYYY 或 MM/DD 即可），year > 1900 才算
            parsed = _dt.parse(norm, fuzzy=False)
            if parsed.year >= 1900:
                hits += 1
        except (ValueError, TypeError, OverflowError):
            pass
    return hits / len(sample) >= 0.6


def list_excel_sheets(raw: bytes, sample_rows: int = 3) -> List[Dict[str, Any]]:
    """枚举 xlsx 内所有 sheet 的元信息 + 前 N 行预览（用于让用户选择 sheet）。"""
    out: List[Dict[str, Any]] = []
    try:
        xl = pd.ExcelFile(io.BytesIO(raw))
    except Exception as e:
        return [{"name": "<读取失败>", "error": str(e), "rows": 0, "columns": [], "preview": []}]
    for name in xl.sheet_names:
        try:
            df = xl.parse(name)
            df.columns = [str(c) for c in df.columns]
            preview = []
            for _, r in df.head(sample_rows).iterrows():
                preview.append({c: _clean(r[c]) for c in df.columns})
            out.append({
                "name": str(name),
                "rows": int(len(df)),
                "columns": [str(c) for c in df.columns],
                "preview": preview,
            })
        except Exception as e:
            out.append({"name": str(name), "error": str(e), "rows": 0, "columns": [], "preview": []})
    return out


def _read_excel_to_data(
    raw: bytes,
    sheet_name: Optional[str] = None,
    no_header: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """读 xlsx 单个 sheet，返回 (data, raw_text)。

    sheet_name: None / 0 = 第一个 sheet；字符串 = 那个名字的 sheet。
    """
    # None 会让 pd.read_excel 返回所有 sheet 的 dict；这里强制为 0 表示「第一个」
    target = 0 if sheet_name is None else sheet_name
    df = pd.read_excel(
        io.BytesIO(raw),
        sheet_name=target,
        header=None if no_header else 0,
    )
    if no_header:
        df = _normalize_no_header_columns(df)
    data = _df_to_dict(df, infer_dates=True)
    try:
        raw_text = df.to_csv(index=False)
    except Exception:
        raw_text = ""
    return data, raw_text


def _parse_json_object(obj: Any) -> Dict[str, Any]:
    """把 JSON 对象统一成 {columns, rows, count} 形式。"""
    if isinstance(obj, dict) and list(obj.keys()) and isinstance(list(obj.values())[0], list):
        return {
            "columns": list(obj.keys()),
            "rows": [dict(zip(obj.keys(), vals)) for vals in zip(*obj.values())],
            "count": len(next(iter(obj.values()), [])),
        }
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            columns = sorted({k for r in obj for k in r.keys()})
            return {"columns": columns, "rows": obj, "count": len(obj)}
        return {"columns": ["value"], "rows": [{"value": v} for v in obj], "count": len(obj)}
    return {
        "columns": ["value"],
        "rows": [{"value": json.dumps(obj, ensure_ascii=False)}],
        "count": 1,
    }


def _parse_multiple_sheets(
    raw: bytes,
    sheet_names: List[str],
    no_header: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """读取多个 sheet，按用户勾选顺序纵向拼接；union 列；新增 __sheet__ 列标识来源。"""
    dfs: List[pd.DataFrame] = []
    raw_parts: List[str] = []
    for name in sheet_names:
        try:
            data, sheet_raw = _read_excel_to_data(raw, sheet_name=name, no_header=no_header)
        except Exception:
            continue
        # 把 data 还原回 df 以便统一加 __sheet__ 列
        df = pd.DataFrame(data["rows"], columns=data["columns"])
        df["__sheet__"] = str(name)
        dfs.append(df)
        raw_parts.append(f"# Sheet: {name}\n{sheet_raw}")
    if not dfs:
        return {"columns": [], "rows": [], "count": 0}, ""
    combined = pd.concat(dfs, ignore_index=True, sort=False)
    combined.columns = [str(c) for c in combined.columns]
    return _df_to_dict(combined), "\n\n".join(raw_parts)


def parse_upload(
    file_storage,
    no_header: bool = False,
    selected_sheets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    filename = (file_storage.filename or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    raw = file_storage.read()
    raw_text = ""
    if ext in ("xlsx", "xls"):
        if selected_sheets and len(selected_sheets) > 1:
            data, raw_text = _parse_multiple_sheets(raw, selected_sheets, no_header)
        else:
            sheet_name = selected_sheets[0] if selected_sheets else None
            data, raw_text = _read_excel_to_data(raw, sheet_name=sheet_name, no_header=no_header)
    elif ext == "csv":
        text = _decode_text(raw)
        raw_text = text
        if no_header:
            df = pd.read_csv(io.StringIO(text), header=None)
            df = _normalize_no_header_columns(df)
        else:
            df = pd.read_csv(io.StringIO(text))
        data = _df_to_dict(df)
    elif ext == "json":
        raw_text = _decode_text(raw)
        data = _parse_json_object(json.loads(raw_text))
    else:
        raw_text = _decode_text(raw)
        data = parse_text(raw_text, no_header=no_header)
    data["source"] = f"file:{filename}"
    data["description"] = describe(data)
    data["raw_text"] = raw_text
    data["source_ext"] = ext
    data["no_header"] = bool(no_header)
    return data


def _normalize_no_header_columns(df: pd.DataFrame) -> pd.DataFrame:
    """为没有表头的数据生成占位列名：字段1, 字段2, ...（中文更友好）。"""
    n = df.shape[1]
    df = df.copy()
    df.columns = [f"字段{i+1}" for i in range(n)]
    return df


def parse_data_text(text: str, no_header: bool = False) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = _parse_json_object(json.loads(text))
            data["source"] = "text:json"
            data["description"] = describe(data)
            data["raw_text"] = text
            data["source_ext"] = "json"
            return data
        except Exception:
            pass
    data = parse_text(text, no_header=no_header)
    data["source"] = "text"
    data["description"] = describe(data)
    data["raw_text"] = text
    data["source_ext"] = "txt"
    data["no_header"] = bool(no_header)
    return data


def parse_text(text: str, no_header: bool = False) -> Dict[str, Any]:
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
            if no_header:
                # 没有表头：所有行都是数据，列数取最大列宽
                width = max((len(r) for r in rows), default=0)
                cols = [f"字段{i+1}" for i in range(width)]
                data_rows = []
                for r in rows:
                    d = {cols[i]: (r[i] if i < len(r) else None) for i in range(width)}
                    data_rows.append(d)
                return {"columns": cols, "rows": data_rows, "count": len(data_rows)}
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
        d = {cols[i]: (parts[i] if i < len(parts) else None) for i in range(len(first))}
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
