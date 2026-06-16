#!/usr/bin/env python3
"""数据预处理：把 prompt 里关于数据的指令（保留两位小数、单位换算、过滤、聚合等）
解析为规则并应用到 data 上，让图表生成时拿到的是已经处理过的数据。

设计目标：
- 完全本地、纯规则（不调 LLM），< 50ms 完成
- 对 prompt 容错高：中文 / 英文 / 中英混排都能识别
- 失败/歧义时安全 no-op，并记录 log 让前端展示「跳过/未识别」

支持的规则（按 prompt 里出现顺序无关；按下面优先级生效）：
  1. round / round_col    —— 数值四舍五入到 N 位小数
  2. drop_null            —— 去除空值行
  3. dedup                —— 去重
  4. iqr_outlier          —— 1.5×IQR 之外的视为离群，整行删除
  5. group_sum / group_mean —— 按某列分组聚合
  6. sort                 —— 按某列升/降序
  7. top_n                —— 取最大/最小的前 N 行
  8. strip_thousands      —— 去掉字符串里的千分位逗号
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------- helpers ----------------------
def _col_name(c) -> str:
    """兼容两种列描述：纯字符串 / {name, type, role, description}。"""
    if isinstance(c, dict):
        return str(c.get("name") or "")
    return str(c or "")


def _try_number(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s2 = s
    for ch in (",", " ", "万", "亿", "%", "元", "¥", "$"):
        s2 = s2.replace(ch, "")
    if s2 in ("", "-", "—"):
        return None
    try:
        return float(s2)
    except (ValueError, TypeError):
        return None


def _is_null(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ("", "-", "—", "N/A", "n/a", "null", "None", "NaN"):
        return True
    if isinstance(v, float) and v != v:  # NaN
        return True
    return False


def _is_numeric_col(rows: List[dict], name: str, threshold: float = 0.7) -> bool:
    vals = [r.get(name) for r in rows if not _is_null(r.get(name))]
    if not vals:
        return False
    num = sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool))
    s_num = sum(1 for v in vals if isinstance(v, str) and _try_number(v) is not None)
    return (num + s_num) / len(vals) >= threshold


def _first_numeric_column(rows: List[dict], columns: list) -> Optional[str]:
    for c in columns:
        name = _col_name(c)
        if name and _is_numeric_col(rows, name):
            return name
    for c in columns:
        name = _col_name(c)
        if name:
            return name
    return None


def _sort_key_for(r: dict, col: str, asc: bool):
    v = r.get(col)
    n = _try_number(v)
    if n is None:
        s = "" if v is None else str(v)
        return (1, s)
    return (0, n if asc else -n)


def _round_value(v, n: int):
    """把单值按 N 位小数四舍五入；非数值返回原值。返回 (new_value, changed)。"""
    if isinstance(v, bool):
        return v, False
    if isinstance(v, (int, float)):
        rounded = round(float(v), n)
        return rounded, rounded != v
    if isinstance(v, str):
        num = _try_number(v)
        if num is not None:
            rounded = round(num, n)
            return rounded, rounded != num
    return v, False


def _apply_numeric_round_to_row(row: dict, decimals: int, only_col: Optional[str] = None):
    """对一行做 round：only_col 指定时只处理该列；否则处理所有列。返回 (new_row, affected_count)。"""
    affected = 0
    new_row = dict(row)
    for k, v in row.items():
        if only_col is not None and k != only_col:
            continue
        new_v, changed = _round_value(v, decimals)
        if changed:
            affected += 1
        new_row[k] = new_v
    return new_row, affected


def _other_numeric_cols(rows: List[dict], columns: list, exclude: str) -> List[str]:
    """所有数值列中排除掉指定列。"""
    return [
        _col_name(c)
        for c in columns
        if _col_name(c) and _col_name(c) != exclude and _is_numeric_col(rows, _col_name(c))
    ]


def _group_aggregate(rows: List[dict], columns: list, by, *, agg: str):
    """按列 by 聚合（sum 或 mean）。返回 (new_rows, info)。"""
    if not by or not any(_col_name(c) == by for c in columns):
        verb = "求和" if agg == "sum" else "求平均"
        return rows, {"action": f"分组{verb}：找不到列「{by}」", "skipped": True}
    other_cols = _other_numeric_cols(rows, columns, exclude=by)
    verb = "求和" if agg == "sum" else "求平均"
    if not other_cols:
        return rows, {"action": f"分组{verb}：没有其它数值列", "skipped": True}
    from collections import OrderedDict

    sums: "OrderedDict[Any, Dict[str, float]]" = OrderedDict()
    counts: "OrderedDict[Any, Dict[str, int]]" = OrderedDict()
    for r in rows:
        key = r.get(by)
        if key not in sums:
            sums[key] = {c: 0.0 for c in other_cols}
            counts[key] = {c: 0 for c in other_cols}
        for c in other_cols:
            num = _try_number(r.get(c))
            if num is not None:
                sums[key][c] += num
                counts[key][c] += 1

    new_rows: List[dict] = []
    for k in sums.keys():
        row = {by: k}
        for c in other_cols:
            cnt = counts[k][c]
            if agg == "sum":
                row[c] = sums[k][c]
            else:  # mean
                row[c] = round(sums[k][c] / cnt, 4) if cnt > 0 else None
        new_rows.append(row)
    return new_rows, {
        "action": f"按「{by}」分组{verb}（{len(other_cols)} 个数值列 → {len(new_rows)} 组）"
    }


# ---------------------- Chinese number normalization ----------------------
# 把 prompt 中「保留两位小数」/「前十」这类中文数字归一化为阿拉伯数字，
# 下面的规则正则才认得「2」「10」。
#
# 关键约束：1 位的中文数字（"一"/"两"/...）很容易出现在列名里（「一月份」、
# 「一班」），所以单字替换要求后跟「量词/分隔/结尾」，避免误伤。
_CN_DIGIT_MAP = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1,
    "二": 2, "贰": 2, "两": 2, "兩": 2,
    "三": 3, "叁": 3, "参": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
    "十": 10, "拾": 10,
}
_CN_DIGIT_CHARS = "".join(_CN_DIGIT_MAP.keys())
# 单字中文数字后必须紧跟这些字符之一，才会被归一化（避免破坏「一月份」之类）
_CN_TERMINATOR = r"(?:位|个|名|行|条|家|项|大|小|高|低|[，。；,;:\s]|$)"


def _cn_to_arabic(s: str):
    """把 1~3 位的简单中文数字字符串转成阿拉伯数字（0-99）。失败返回 None。"""
    if not s or not all(c in _CN_DIGIT_MAP for c in s):
        return None
    if len(s) == 1:
        return _CN_DIGIT_MAP[s]
    if len(s) == 2:
        a, b = _CN_DIGIT_MAP[s[0]], _CN_DIGIT_MAP[s[1]]
        if s[0] == "十":                # 十二 → 12
            return 10 + b
        if s[1] == "十":                # 二十 → 20
            return a * 10
    if len(s) == 3 and s[1] == "十":   # 二十三 → 23
        a, b = _CN_DIGIT_MAP[s[0]], _CN_DIGIT_MAP[s[2]]
        return a * 10 + b
    return None


def _normalize_cn_numbers(prompt: str) -> str:
    """把 prompt 里独立的中文数字替换为阿拉伯数字。

    例：
        "保留两位小数"   → "保留2位小数"
        "前十"          → "前10"         (十 后无单位，但结尾 $ 也是终止符)
        "前十二大"       → "前12大"        (2 字串默认独立)
        "一月份"         → "一月份"        (月 不是终止符，保留)
        "销售额保留两位"  → "销售额保留2位"  (两 后跟位)
    """
    if not prompt:
        return prompt

    def _repl(m):
        s = m.group(0)
        n = _cn_to_arabic(s)
        return str(n) if n is not None else s

    # 1) 2~3 位的连续中文数字：基本是独立数字（"十二" "二十三"），可直接转
    prompt = re.sub(f"[{_CN_DIGIT_CHARS}]{{2,3}}", _repl, prompt)
    # 2) 1 位的中文数字：要求后面是「量词/分隔/结尾」，避免破坏列名
    prompt = re.sub(f"[{_CN_DIGIT_CHARS}](?={_CN_TERMINATOR})", _repl, prompt)
    return prompt


# ---------------------- rule parsing ----------------------
def _parse_rules(prompt: str) -> List[Dict[str, Any]]:
    p = (prompt or "").strip()
    if not p:
        return []
    # 「保留两位小数」这种中文数字写法归一化为阿拉伯数字（"两"→2、"十二"→12 …）
    p = _normalize_cn_numbers(p)
    rules: List[Dict[str, Any]] = []

    # 1) 「<列> 保留 N 位小数」 —— 列级
    #    列名必须是下列三种之一，避免「请保留」这种句首虚词被误识别为列名：
    #      a) 引号包裹： "X" / 'X' / 「X」 / "X"
    #      b) "把 X 保留 ..."  句式
    #      c) 纯 ASCII 标识符（避免与中文句子虚词混淆）
    m = re.search(
        r"(?:"
        r"[\"「\"\u201c]([\w\u4e00-\u9fa5]+)[\"」\"\u201d]"   # "X" 形式
        r"|把\s*([\w\u4e00-\u9fa5]+)\s*"                       # 把 X 形式
        r"|\b([A-Za-z_][\w]*)\b"                                # 纯 ASCII 词
        r")\s*(?:保留|精确到|保留到|round\s*to)\s*(\d+)\s*(?:位\s*小数|位\s*有效数字|decimals?|decimal\s*places?)",
        p,
        re.I,
    )
    if m:
        col = m.group(1) or m.group(2) or m.group(3)
        rules.append({"type": "round_col", "column": col, "decimals": int(m.group(4))})

    # 2) 「保留 N 位小数 / round to N decimals」 —— 全表
    #    若已经识别到列级 round_col，则不再追加全局 round
    if not any(r["type"] in ("round", "round_col") for r in rules):
        m = re.search(
            r"(?:保留|精确到|保留到|round(?:ed)?\s*to)\s*(\d+)\s*(?:位\s*小数|位\s*有效数字|decimals?|decimal\s*places?)",
            p,
            re.I,
        )
        if m:
            rules.append({"type": "round", "decimals": int(m.group(1))})
        elif re.search(r"(?:取整|保留\s*整数|不要小数|精确到\s*个位|round\s*to\s*integer)", p, re.I):
            rules.append({"type": "round", "decimals": 0})

    # 3) 去掉千分位
    if re.search(r"(?:去掉|去除|不要|移除|strip)\s*(?:千分位|千位分隔符|逗号|thousands?)", p, re.I):
        rules.append({"type": "strip_thousands"})

    # 4) 去除空值
    if re.search(
        r"(?:去除|删除|过滤|排除|去掉|不要|drop|remove|filter\s*out|exclude)\s*"
        r"(?:空值|空行|null|NaN|缺失|空|empty|nulls?|none|missing)",
        p,
        re.I,
    ):
        rules.append({"type": "drop_null"})

    # 5) 去重
    if re.search(r"(?:去重|去除\s*重复|删除\s*重复|过滤\s*重复|去掉\s*重复|dedup|distinct)", p, re.I):
        rules.append({"type": "dedup"})

    # 6) 异常值 / 离群点
    if re.search(r"(?:去除|剔除|过滤|去掉).*?(?:异常值|离群|异常|outliers?|IQR)", p, re.I):
        rules.append({"type": "iqr_outlier"})

    # 7) 聚合：按 X 分组求和 / 求平均
    #    用非贪婪匹配防止列名吃掉后续关键字（如 "category分组求和" 被吃成 "category分组求和"）
    m = re.search(
        r"按\s*([\w\u4e00-\u9fa5]+?)\s*(?:分组\s*)?求\s*(?:和|总和)|group\s*by\s*([\w\u4e00-\u9fa5]+?)\s*sum",
        p,
        re.I,
    )
    if m:
        by = m.group(1) or m.group(2)
        if by:
            rules.append({"type": "group_sum", "by": by})
    m = re.search(
        r"按\s*([\w\u4e00-\u9fa5]+?)\s*(?:分组\s*)?求\s*(?:平均|均值|avg|mean)|group\s*by\s*([\w\u4e00-\u9fa5]+?)\s*(?:avg|mean)",
        p,
        re.I,
    )
    if m:
        by = m.group(1) or m.group(2)
        if by:
            rules.append({"type": "group_mean", "by": by})

    # 8) 排序 sort by X asc/desc
    m = re.search(
        r"按\s*[\"「\"\"'']?([\w\u4e00-\u9fa5]+)[\"」\"\"'']?\s*(升序|降序|从\s*小\s*到\s*大|从\s*大\s*到\s*小|asc(?:ending)?|desc(?:ending)?)",
        p,
        re.I,
    )
    if m:
        col, dir_ = m.group(1), m.group(2).lower()
        asc = dir_.startswith(("asc", "升", "从小到大"))
        rules.append({"type": "sort", "column": col, "ascending": asc})
    else:
        m = re.search(
            r"sort\s*by\s*([\w\u4e00-\u9fa5]+)\s*(asc(?:ending)?|desc(?:ending)?)", p, re.I
        )
        if m:
            rules.append(
                {
                    "type": "sort",
                    "column": m.group(1),
                    "ascending": m.group(2).lower().startswith("asc"),
                }
            )

    # 9) Top N
    #   9a) 按 X 取前 N (大/小 可选)
    m = re.search(
        r"按\s*([\w\u4e00-\u9fa5]+)\s*(?:取\s*)?(?:前|TOP|top)\s*(\d+)\s*(大|小|高|低|个|名|位|行|条|家|项)?",
        p,
        re.I,
    )
    if m:
        col, n, q = m.group(1), int(m.group(2)), m.group(3) or ""
        asc = bool(re.search(r"小|低|asc", q, re.I))
        rules.append({"type": "top_n", "n": n, "by": col, "ascending": asc})
    else:
        #   9b) 前 N 大/小（无指定列 → 取第一个数值列）
        m = re.search(r"(?:前|TOP|top)\s*(\d+)\s*(大|小|高|低|个|名|位|行|条|家|项)?", p, re.I)
        if m:
            n, q = int(m.group(1)), m.group(2) or ""
            asc = bool(re.search(r"小|低|asc", q, re.I))
            rules.append({"type": "top_n", "n": n, "ascending": asc})

    return rules


# 规则按此优先级生效（与 prompt 中出现顺序无关）
_RULE_PRIORITY = [
    "strip_thousands",
    "round",
    "round_col",
    "drop_null",
    "dedup",
    "iqr_outlier",
    "group_sum",
    "group_mean",
    "sort",
    "top_n",
]


# ---------------------- rule application ----------------------
def _apply_rule(rule: dict, rows: List[dict], columns: list) -> Tuple[List[dict], Dict[str, Any]]:
    t = rule["type"]

    if t == "round":
        n = int(rule.get("decimals", 2))
        affected = 0
        new_rows: List[dict] = []
        for r in rows:
            new_r, n_changed = _apply_numeric_round_to_row(r, n)
            affected += n_changed
            new_rows.append(new_r)
        return new_rows, {"action": f"所有数值四舍五入到 {n} 位小数（{affected} 处）"}

    if t == "round_col":
        col = rule.get("column")
        n = int(rule.get("decimals", 2))
        if not any(_col_name(c) == col for c in columns):
            return rows, {"action": f"列「{col}」保留 {n} 位小数：找不到列", "skipped": True}
        affected = 0
        new_rows = []
        for r in rows:
            new_r, n_changed = _apply_numeric_round_to_row(r, n, only_col=col)
            affected += n_changed
            new_rows.append(new_r)
        return new_rows, {"action": f"列「{col}」保留 {n} 位小数（{affected} 处）"}

    if t == "drop_null":
        before = len(rows)
        new_rows = [r for r in rows if not any(_is_null(r.get(k)) for k in r.keys())]
        dropped = before - len(new_rows)
        return new_rows, {"action": f"去除空值行 {dropped} 行（{before} → {len(new_rows)}）"}

    if t == "dedup":
        seen = set()
        new_rows = []
        for r in rows:
            key = tuple(sorted((str(k), str(r.get(k))) for k in r.keys()))
            if key in seen:
                continue
            seen.add(key)
            new_rows.append(r)
        dropped = len(rows) - len(new_rows)
        return new_rows, {"action": f"去重 {dropped} 行（{len(rows)} → {len(new_rows)}）"}

    if t == "iqr_outlier":
        num_cols = [
            _col_name(c) for c in columns if _col_name(c) and _is_numeric_col(rows, _col_name(c))
        ]
        if not num_cols:
            return rows, {"action": "IQR 异常值：找不到数值列", "skipped": True}
        ranges: Dict[str, Tuple[float, float]] = {}
        for col in num_cols:
            nums = sorted(
                n for n in (_try_number(r.get(col)) for r in rows) if n is not None
            )
            if len(nums) < 4:
                continue
            n = len(nums)
            q1, q3 = nums[n // 4], nums[(3 * n) // 4]
            iqr = q3 - q1
            if iqr <= 0:
                continue
            ranges[col] = (q1 - 1.5 * iqr, q3 + 1.5 * iqr)
        if not ranges:
            return rows, {"action": "IQR 异常值：无有效分布", "skipped": True}
        new_rows = []
        for r in rows:
            outlier = False
            for col, (lo, hi) in ranges.items():
                v = _try_number(r.get(col))
                if v is not None and (v < lo or v > hi):
                    outlier = True
                    break
            if not outlier:
                new_rows.append(r)
        dropped = len(rows) - len(new_rows)
        return new_rows, {
            "action": f"IQR 异常值：删除 {dropped} 行（{len(num_cols)} 个数值列参与检测）"
        }

    if t == "group_sum":
        return _group_aggregate(rows, columns, rule.get("by"), agg="sum")

    if t == "group_mean":
        return _group_aggregate(rows, columns, rule.get("by"), agg="mean")

    if t == "sort":
        col = rule.get("column")
        asc = rule.get("ascending", True)
        if not col:
            return rows, {"action": "排序：未指定列", "skipped": True}
        if not any(_col_name(c) == col for c in columns):
            return rows, {"action": f"排序：找不到列「{col}」", "skipped": True}
        new_rows = sorted(rows, key=lambda r: _sort_key_for(r, col, asc))
        return new_rows, {"action": f"按「{col}」{'升' if asc else '降'}序排列"}

    if t == "top_n":
        n = int(rule.get("n", 10))
        asc = rule.get("ascending", False)
        by_col = rule.get("by")
        if not by_col:
            by_col = _first_numeric_column(rows, columns)
        if not by_col:
            return rows, {"action": f"前 {n}：找不到数值列", "skipped": True}
        new_rows = sorted(rows, key=lambda r: _sort_key_for(r, by_col, asc))[:n]
        return new_rows, {
            "action": f"取{'最小' if asc else '最大'} {n} 行（按「{by_col}」）"
        }

    if t == "strip_thousands":
        affected = 0
        new_rows = []
        for r in rows:
            new_r = dict(r)
            for k, v in r.items():
                if isinstance(v, str) and "," in v:
                    num = _try_number(v)
                    if num is not None and not _is_null(v):
                        new_r[k] = num
                        affected += 1
            new_rows.append(new_r)
        return new_rows, {"action": f"去掉千分位 {affected} 处"}

    return rows, {"action": f"未知规则：{t}", "skipped": True}


# ---------------------- main entry ----------------------
def preprocess_data(prompt: str, data: dict) -> Tuple[dict, dict]:
    """主入口：解析 prompt 中的数据处理指令并应用到 data。

    Returns:
        (new_data, info)，其中：
          - new_data: 处理后的数据（未识别到任何规则时与原数据等价，仅复制）
          - info: {
              "rules": 识别到的规则名（type 列表），
              "applied": [{ "type", "action", "before", "after", ... }]，
              "skipped": [{ "type", "error" }]，
              "summary": "中文一句话总结做了什么"
            }
    """
    if not data or not data.get("rows"):
        return data, {
            "rules": [],
            "applied": [],
            "skipped": [],
            "summary": "无可预处理的数据",
        }

    rules = _parse_rules(prompt or "")
    if not rules:
        return data, {
            "rules": [],
            "applied": [],
            "skipped": [],
            "summary": "未识别到数据预处理指令",
        }

    # 按优先级排序，与 prompt 中出现顺序无关
    rules.sort(
        key=lambda r: _RULE_PRIORITY.index(r["type"])
        if r["type"] in _RULE_PRIORITY
        else 99
    )

    rows = [dict(r) for r in (data.get("rows") or [])]
    columns = data.get("columns") or []
    applied: List[dict] = []
    skipped: List[dict] = []

    for rule in rules:
        try:
            before = len(rows)
            new_rows, info = _apply_rule(rule, rows, columns)
            after = len(new_rows)
            applied.append({"type": rule["type"], "before": before, "after": after, **info})
            rows = new_rows
        except Exception as e:
            skipped.append({"type": rule["type"], "error": str(e)})

    summary_parts = [
        a.get("action")
        for a in applied
        if a.get("action") and not a.get("skipped")
    ]
    summary = "；".join(p for p in summary_parts if p) if summary_parts else "已尝试处理但无变化"
    if skipped:
        summary += f"（{len(skipped)} 条规则跳过）"

    new_data = dict(data)
    new_data["rows"] = rows
    new_data["count"] = len(rows)
    if new_data.get("description"):
        new_data["description"] = (
            f"预处理后：{len(rows)} 行 × {len(_column_names(columns))} 列；"
            + (new_data["description"] or "")
        )

    return new_data, {
        "rules": [r["type"] for r in rules],
        "applied": applied,
        "skipped": skipped,
        "summary": summary,
    }


def _column_names(columns) -> List[str]:
    return [n for n in (_col_name(c) for c in columns) if n]
