#!/usr/bin/env python3
"""调用大语言模型：统一走 OpenAI 兼容协议。"""
import json
import urllib.request
import urllib.error
import ssl
from typing import Dict, List, Optional, Any


def _post_chat_completion(cfg: Dict[str, Any], payload: Dict[str, Any], stream: bool):
    """统一的 chat/completions POST 入口。返回 ``urlopen`` 上下文对象。"""
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    if not base_url or not api_key:
        raise RuntimeError("LLM 配置不完整：需要 Base URL、API Key。")
    endpoint = f"{base_url}/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=300, context=ctx)


def _should_drop_response_format(err: Exception) -> bool:
    """HTTP 400 / 422 且错误文本里出现 response_format 相关字眼 → True。"""
    msg = str(err) or ""
    low = msg.lower()
    if "response_format" not in low and "json_schema" not in low and "json_object" not in low:
        return False
    if "http 400" in low or "http 422" in low or "unsupported" in low or "unknown" in low or "invalid" in low:
        return True
    return False


def _detect_provider(base_url: str) -> str:
    """从 Base URL 嗅探 provider，用于调整 thinking 字段语义。

    - 命中 ``:11434``（Ollama 默认端口）或路径里含 ``/ollama`` → ``"ollama"``
    - 命中 ``bigmodel.cn`` / ``zhipu`` / ``zhipuai`` → ``"glm"``（智谱 GLM-4.5+）
    - 其它 → ``"openai"``（含 OpenAI / DeepSeek / DashScope 等 OpenAI 兼容服务）
    """
    u = (base_url or "").lower()
    if ":11434" in u or "/ollama" in u:
        return "ollama"
    if "bigmodel.cn" in u or "zhipuai" in u or "zhipu" in u:
        return "glm"
    return "openai"


def _resolve_thinking_field(cfg: Dict[str, Any], user_value: Optional[str]):
    """把 DB 里的 ``llm_thinking`` 按 provider 转成 ``(field, value)``，无则返回 ``None``。

    Provider 差异：
    - **openai**（含 DeepSeek / DashScope / Qwen 等 OpenAI 兼容）：``off`` → 不发送；
      ``low``/``medium``/``high`` → 透传 ``reasoning_effort``
    - **ollama**：``off`` → 发 ``reasoning_effort: "none"`` 显式关闭（Ollama 缺省≠关闭）；
      其它值 → 透传
    - **glm**（智谱 GLM-4.5+）：用 ``thinking: {type: "enabled" | "disabled"}`` 对象；
      ``off`` → ``{type: "disabled"}``；其它值（low/medium/high）→ ``{type: "enabled"}``（GLM 无粒度，统一开启）
    - 非法值 → ``None``
    """
    v = (user_value or "").strip().lower()
    provider = (cfg.get("provider") or _detect_provider(cfg.get("base_url", ""))).lower()
    if v in ("", "off"):
        if provider == "ollama":
            return ("reasoning_effort", "none")
        if provider == "glm":
            return ("thinking", {"type": "disabled"})
        return None
    if v in ("low", "medium", "high"):
        if provider == "glm":
            return ("thinking", {"type": "enabled"})
        return ("reasoning_effort", v)
    return None


def _build_payload(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int],
    temperature: Optional[float],
    response_format: Optional[Dict[str, Any]],
    stream: bool,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": cfg.get("model") or "",
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format is not None:
        payload["response_format"] = response_format
    thinking = _resolve_thinking_field(cfg, reasoning_effort)
    if thinking is not None:
        field, value = thinking
        payload[field] = value
    if stream:
        payload["stream"] = True
    return payload


def _extract_content_text(obj: Dict[str, Any]) -> str:
    """从 chat/completions 响应里抠出 assistant 的文本内容。"""
    if "choices" in obj and obj["choices"]:
        msg = obj["choices"][0].get("message") or {}
        return msg.get("content") or ""
    if "content" in obj and isinstance(obj["content"], list):
        return "".join((c.get("text") or "") for c in obj["content"])
    if "content" in obj:
        return str(obj["content"])
    raise RuntimeError(f"未知响应结构：{json.dumps(obj)[:500]}")


def _extract_reasoning_text(obj: Dict[str, Any]) -> str:
    """从响应里抽 ``reasoning`` / ``reasoning_content``（Ollama / OpenAI 推理模式都可能用）。

    优先 ``choices[0].message.reasoning``，其次 ``reasoning_content``；返回空串表示没有。
    """
    if "choices" in obj and obj["choices"]:
        msg = obj["choices"][0].get("message") or {}
        return (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    return ""


def call_llm(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """调用大模型（一次性）。**只返回 content 文本**，thinking 字段忽略。

    完整响应解析需要 reasoning 时，用 :func:`call_llm_raw`。
    """
    content, _ = call_llm_raw(cfg, messages, max_tokens, temperature,
                              response_format, reasoning_effort)
    return content


def call_llm_raw(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, str]:
    """调用大模型（一次性），返回 ``(content, reasoning)``。

    - ``content``：assistant 的文本内容（用于 JSON 解析 / 选类型等）
    - ``reasoning``：Ollama / OpenAI 推理模式下的思考过程（可空字符串）；
      存进 ``raw_reply`` 给用户事后查看
    """
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM 配置不完整：需要 Base URL、API Key、Model。")

    last_err: Optional[Exception] = None
    cur_rf = response_format
    raw = ""
    for _ in range(3):
        payload = _build_payload(cfg, messages, max_tokens, temperature, cur_rf, stream=False, reasoning_effort=reasoning_effort)
        try:
            with _post_chat_completion(cfg, payload, stream=False) as resp:
                raw = resp.read().decode("utf-8")
            obj = json.loads(raw)
            return _extract_content_text(obj), _extract_reasoning_text(obj)
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"LLM 服务 HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}")
            if cur_rf is not None and _should_drop_response_format(last_err):
                cur_rf = {"type": "json_object"} if cur_rf.get("type") == "json_schema" else None
                continue
            raise last_err
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 LLM 服务：{e}")
        except Exception as e:
            raise RuntimeError(f"LLM 返回不是合法 JSON：{e}; body={raw[:500]}")
    if last_err:
        raise last_err
    raise RuntimeError("LLM 调用失败：未知原因")


def call_llm_stream(
    cfg: Dict[str, Any],
    messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_format: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
):
    """流式调用大模型，逐 chunk 产出 content。

    设计要点：
    - 走 OpenAI 兼容协议，请求时带 ``stream: true``，要求 ``text/event-stream``。
    - 按 SSE 规范解析 ``data: {json}`` 增量行，遇到 ``data: [DONE]`` 结束。
    - 如果服务端直接返回 ``application/json``（不支持流式），自动降级为一次性 yield 全部内容。
    - ``response_format`` 服务端拒绝时按 ``json_schema`` → ``json_object`` → 不带降级重试一次；
      流已开始则不再降级。
    - 与 ``call_llm`` 错误处理对齐：HTTP 错误抛 ``RuntimeError``，由调用方决定回退。
    - ``reasoning_effort`` 与 ``call_llm`` 同义；空值不发送。

    Yields:
        str: 每次产出的 content delta（可能是空字符串，调用方需自行过滤）。
    """
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    if not base_url or not api_key or not model:
        raise RuntimeError("LLM 配置不完整：需要 Base URL、API Key、Model。")

    last_err: Optional[Exception] = None
    cur_rf = response_format
    for _ in range(3):
        payload = _build_payload(cfg, messages, max_tokens, temperature, cur_rf, stream=True, reasoning_effort=reasoning_effort)
        try:
            resp = _post_chat_completion(cfg, payload, stream=True)
            break
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"LLM 服务 HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}")
            if cur_rf is not None and _should_drop_response_format(last_err):
                cur_rf = {"type": "json_object"} if cur_rf.get("type") == "json_schema" else None
                continue
            raise last_err
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 LLM 服务：{e}")
    else:
        if last_err:
            raise last_err
        raise RuntimeError("LLM 调用失败：未知原因")

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        # 服务端未走 SSE：当成一次性 JSON，把全部 content 作为单 chunk yield
        raw_text = ""
        try:
            raw_text = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw_text)
        except Exception as e:
            raise RuntimeError(f"LLM 返回不是合法 JSON：{e}; body={raw_text[:500]}")
        finally:
            try:
                resp.close()
            except Exception:
                pass
        msg = (obj.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        if not text and isinstance(obj.get("content"), list):
            text = "".join((c.get("text") or "") for c in obj["content"])
        if not text and isinstance(obj.get("content"), str):
            text = obj["content"]
        if text:
            yield text
        return

    # 真正的 SSE 路径
    try:
        for raw_line in resp:
            try:
                line = raw_line.decode("utf-8", errors="replace")
            except Exception:
                continue
            if not line:
                continue
            # SSE 行以 "data: " 开头；忽略 ":..." 注释行和事件名行
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield content
            # 推理模型（Ollama / OpenAI o-series）会在 delta 里同时流 thinking；按需取
            # reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            # if reasoning:
            #     yield ("reasoning", reasoning)
            # 部分实现在最后一个 chunk 把 finish_reason 放进 delta；这里不专门处理
    finally:
        try:
            resp.close()
        except Exception:
            pass


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
    reply = call_llm(
        cfg,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=200,
        temperature=0.3,
        reasoning_effort=cfg.get("reasoning_effort") or None,
    )
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
        "请严格按 response_format 给定的 JSON schema 输出（不要写 Markdown 代码块、不要任何前后缀文字），"
        "其中：\n"
        "1) option 是完整的 ECharts 配置对象（title/tooltip/legend/grid/xAxis/yAxis/series/color 等），"
        "series.data 填入真实数值；\n"
        "2) xAxis/yAxis/legend 的内容与数据列名一致；\n"
        "3) 标题/副标题可以留空；\n"
        "4) 禁止 JS 函数字面量（JSON 不支持函数），自定义 formatter 用 ECharts 字符串模板"
        "（如 '{b}: {c}'），自定义配色用 series 顶层 color: [...] 数组或省略；\n"
        "5) content 字段填 30-80 字中文文字解释该图表表达的核心信息（最大/最小/趋势/对比），"
        "不要复述数据，不要 markdown 格式。"
    )

    if knowledge:
        pieces.append("【ECharts 配置项指导】")
        for section_name, content in knowledge.items():
            pieces.append(f"-- {section_name} --")
            pieces.append(content)

    return "\n\n".join(pieces)
