#!/usr/bin/env python3
"""调用大语言模型：统一走 OpenAI 兼容协议。"""
import json
import urllib.request
import urllib.error
import ssl
from typing import Dict, List, Optional, Any


def call_llm(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM 配置不完整：需要 Base URL、API Key、Model。")
    endpoint = f"{base_url}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM 服务 HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 LLM 服务：{e}")

    try:
        obj = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"LLM 返回不是合法 JSON：{e}; body={raw[:500]}")

    # OpenAI / 兼容接口
    try:
        if "choices" in obj and obj["choices"]:
            msg = obj["choices"][0].get("message") or {}
            return msg.get("content") or ""
        # Anthropic 兼容
        if "content" in obj and isinstance(obj["content"], list):
            return "".join((c.get("text") or "") for c in obj["content"])
        if "content" in obj:
            return str(obj["content"])
        raise RuntimeError(f"未知响应结构：{raw[:500]}")
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"解析 LLM 响应失败：{e}; body={raw[:500]}")


def pick_chart_type(
    cfg: Dict[str, Any],
    prompt: str,
    data: Optional[Dict[str, Any]],
    hint: str,
):
    """让 LLM 根据需求和数据推荐一个图表类型。"""
    if hint:
        return hint, "用户指定图表类型。"

    data_desc = ""
    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            cols = ", ".join(str(c.get("name") or "") for c in raw_cols)
        else:
            cols = ", ".join(str(c) for c in raw_cols)
        sample = json.dumps(data["rows"][:3], ensure_ascii=False)
        extra = ""
        if data.get("summary"):
            extra = f"\n数据集摘要：{data['summary']}"
        data_desc = f"\n数据字段：{cols}\n样例：{sample}\n行数：{len(data['rows'])}{extra}"

    system = (
        "你是一个专业的数据可视化助手，请根据用户的需求与数据，推荐一个最合适的 ECharts 图表类型。"
        "请只回复一个英文单词，从以下类型中选择：bar, line, pie, scatter, radar, gauge, funnel, candlestick, heatmap, sunburst, treemap, sankey, boxplot, pictorialBar, effectScatter。"
        "随后用一行中文简短解释选择理由。"
        "示例输出：\nbar\n用于比较不同类别之间的数值大小，适合该场景的分类数据对比。"
    )
    user = f"用户需求：{prompt or '根据提供的数据自动生成合适的图表'}{data_desc}\n\n请给出推荐图表类型与理由。"
    reply = call_llm(cfg, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=200, temperature=0.3)
    lines = [ln.strip() for ln in reply.strip().splitlines() if ln.strip()]
    chart_type = lines[0].lower() if lines else "bar"
    allowed = {"bar","line","pie","scatter","radar","gauge","funnel","candlestick","heatmap","sunburst","treemap","sankey","boxplot","pictorialBar","effectScatter"}
    if chart_type not in allowed:
        chart_type = "bar"
    reason = " ".join(lines[1:]) or "自动选择。"
    return chart_type, reason


def _percentile(sorted_nums: List[float], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认行为一致）。"""
    if not sorted_nums:
        return 0.0
    n = len(sorted_nums)
    if n == 1:
        return sorted_nums[0]
    k = (n - 1) * q
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_nums[f]
    return sorted_nums[f] + (sorted_nums[c] - sorted_nums[f]) * (k - f)


def compute_column_stats(
    rows: List[Dict[str, Any]],
    column_names: List[str],
) -> str:
    """给一份行数据算每列的紧凑统计，喂给 LLM 让它对「全量」有概念。

    - 每列一行：列名 (类型) + 范围 / 分布 / distinct / top-k
    - 数值列：min / max / mean / median / p25 / p75 / IQR（>100 行）
    - 文本列：distinct / top-3 with counts
    - 总体限 ~ 200 字符/列，top-k 至多 3 项

    Returns "" 当行数太少（< 20）或没有列 —— 小数据直接看样本就够了。
    """
    if not rows or not column_names or len(rows) < 20:
        return ""

    def _try_float(v: Any) -> Optional[float]:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace(",", "").replace(" ", "")
        if s in ("", "-", "—"):
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    def _fmt_num(x: float) -> str:
        # 自动取整显示：整数 1234.0 → "1234"；小数 → "1.23"
        if x == int(x) and abs(x) < 1e15:
            return f"{int(x):,}"
        return f"{x:,.2f}"

    show_percentiles = len(rows) >= 100
    lines: List[str] = []
    for col in column_names:
        if not col:
            continue
        vals = [r.get(col) for r in rows]
        non_null = [v for v in vals if v is not None and v != ""]
        nulls = len(vals) - len(non_null)

        if not non_null:
            lines.append(f"- {col}: 全空")
            continue

        # 试判数值
        nums: List[float] = []
        for v in non_null:
            fv = _try_float(v)
            if fv is not None:
                nums.append(fv)
        is_numeric = len(nums) >= len(non_null) * 0.7
        null_part = f", {nulls} 空" if nulls else ""

        if is_numeric:
            nums_sorted = sorted(nums)
            distinct = len(set(nums_sorted))
            lo, hi = nums_sorted[0], nums_sorted[-1]
            range_str = f"={_fmt_num(lo)}" if lo == hi else f"{_fmt_num(lo)}–{_fmt_num(hi)}"
            mean = sum(nums) / len(nums)
            parts = [f"范围 {range_str}", f"均值 {_fmt_num(mean)}", f"{distinct} distinct"]
            if show_percentiles:
                med = _percentile(nums_sorted, 0.5)
                p25 = _percentile(nums_sorted, 0.25)
                p75 = _percentile(nums_sorted, 0.75)
                iqr = p75 - p25
                parts.append(f"中位数 {_fmt_num(med)}")
                parts.append(f"p25-p75 {_fmt_num(p25)}–{_fmt_num(p75)}")
                if iqr > 0 and distinct > 4:
                    parts.append(f"IQR {_fmt_num(iqr)}")
            line = f"- {col} (数字, {len(non_null)} 值{null_part}): " + ", ".join(parts)
        else:
            from collections import Counter
            cnt = Counter(str(v) for v in non_null)
            distinct = len(cnt)
            top = cnt.most_common(3)
            top_str = ", ".join(f'"{k}"({n})' for k, n in top)
            line = f"- {col} (文本, {len(non_null)} 值{null_part}): {distinct} distinct, top: {top_str}"

        if len(line) > 240:
            line = line[:237] + "..."
        lines.append(line)

    return "\n".join(lines)


def build_chart_prompt(
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str,
    style_hint: Optional[Dict[str, Any]],
    knowledge: Dict[str, Any],
) -> str:
    pieces = []
    pieces.append(f"请使用 Apache ECharts 绘制一张「{chart_type}」类型图表。")
    if prompt:
        pieces.append(f"用户需求：{prompt}")

    if data and data.get("rows"):
        # 兼容两种列描述：纯字符串列表 / 带 schema 的对象列表（来自 LLM 整理）
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            column_names = [str(c.get("name") or "") for c in raw_cols]
            schema_lines = [
                f"- {c.get('name')} ({c.get('type','string')}/{c.get('role','value')}): {c.get('description','')}"
                for c in raw_cols
            ]
            pieces.append("数据 schema：\n" + "\n".join(schema_lines))
        else:
            column_names = [str(c) for c in raw_cols]
        rows = data["rows"]
        pieces.append("数据字段：" + ", ".join(column_names))

        # 1) 整体统计（>20 行才发）—— LLM 能借此理解全量分布
        stats_text = compute_column_stats(rows, column_names)
        if stats_text:
            pieces.append(f"【数据统计摘要 (共 {len(rows)} 行)】\n{stats_text}")

        # 2) 样本行（>100 行只发前 100）
        if len(rows) > 100:
            shown = rows[:100]
            pieces.append(f"数据共 {len(rows)} 行，仅发送前 100 行用于演示；"
                          f"请在生成代码时按相同字段保留完整结构：")
        else:
            shown = rows
        pieces.append("数据 JSON：")
        pieces.append(json.dumps(shown, ensure_ascii=False))

        # 如果有 LLM 整理产出的 summary/notes，附带给 chart 生成模型
        if data.get("summary"):
            pieces.append(f"数据集摘要：{data['summary']}")
        if data.get("notes") and data.get("notes") not in ("", "无"):
            pieces.append(f"数据整理说明：{data['notes']}")

    if style_hint:
        pieces.append(f"样式偏好：{json.dumps(style_hint, ensure_ascii=False)}")

    pieces.append(
        "请严格根据以下「ECharts 配置项指导」生成完整的 option JSON（不要写任何额外的字段注释，"
        "保证 JSON 可直接由 JSON.parse 解析）。请确保：\n"
        "1) series 中的 data 字段填入真实的数值数据；\n"
        "2) xAxis/yAxis 或 legend 的内容与数据列名一致；\n"
        "3) 标题 / 副标题可以留空；\n"
        "4) 只输出一个 JSON 对象，用 ```json ... ``` 包裹。\n"
        "5) 在 JSON 之后再用一小段中文解释该图表表达的要点。"
    )

    if knowledge:
        pieces.append("【ECharts 配置项指导】")
        for section_name, content in knowledge.items():
            pieces.append(f"-- {section_name} --")
            pieces.append(content)

    return "\n\n".join(pieces)
