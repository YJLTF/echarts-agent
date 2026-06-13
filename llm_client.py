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
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
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
        cols = ", ".join(data.get("columns", []))
        sample = json.dumps(data["rows"][:3], ensure_ascii=False)
        data_desc = f"\n数据字段：{cols}\n样例：{sample}\n行数：{len(data['rows'])}"

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
        columns = data["columns"]
        rows = data["rows"]
        pieces.append("数据字段：" + ", ".join(columns))
        # 为了节省 token，超过 100 行则压缩为前 100 行，并告知总行数
        if len(rows) > 100:
            shown = rows[:100]
            pieces.append(f"数据共 {len(rows)} 行，仅发送前 100 行用于演示；"
                          f"请在生成代码时按相同字段保留完整结构：")
        else:
            shown = rows
        pieces.append("数据 JSON：")
        pieces.append(json.dumps(shown, ensure_ascii=False))

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
