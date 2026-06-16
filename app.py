#!/usr/bin/env python3
import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)
from flask_cors import CORS
from werkzeug.datastructures import FileStorage

from llm_client import call_llm, call_llm_stream, call_llm_raw, build_chart_prompt, pick_chart_type
import llm_client
from data_parser import parse_upload, parse_data_text, list_excel_sheets
from data_understanding import understand_data
from data_preprocessing import preprocess_data
from knowledge import search_knowledge, get_knowledge_for_type

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
KB_DIR = os.path.join(BASE_DIR, "knowledge")
DB_PATH = os.path.join(BASE_DIR, "app.db")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "echarts-agent-secret")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
CORS(app)


# ---------------------- Database ----------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                role TEXT,
                content TEXT,
                data_json TEXT,
                option_json TEXT,
                code TEXT,
                chart_type TEXT,
                created_at TEXT,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()


def get_config(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_all_config() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


def config_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        api_key = get_config("llm_api_key")
        if not api_key:
            return (
                jsonify(
                    {
                        "error": "缺少必要配置，请先前往「配置」页面填写 API Key 等信息。",
                        "need_config": True,
                    }
                ),
                428,
            )
        return fn(*args, **kwargs)

    return wrapped


# ---------------------- Utils ----------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on", "y")


def _get_param(name: str):
    """从 form / query / JSON body 中任一处取参，按优先级回退。

    Flask 会在 request 上下文内缓存 ``request.get_json()`` 的结果，
    所以这里多次调用没有重复解析开销。
    """
    return (
        request.form.get(name)
        or request.args.get(name)
        or (request.get_json(silent=True) or {}).get(name)
    )


_DEFAULT_SYSTEM_PROMPT = """你是一名资深的 Apache ECharts 图表工程师，擅长把数据快速转化成生产级 ECharts option。

【硬约束 — 不可违反】
0) **回复首字符必须是 `{`，末字符必须是 `}`，整篇回复只包含一个 JSON 对象**。**严禁**使用 Markdown 代码块、严禁写任何前后缀文字、严禁重复输出多个对象。
1) 严格按 response_format 给定的 JSON schema 输出。
2) 严格使用用户给的数据，不编造、不替换；xAxis/yAxis 的 data 与列名一致；数值不要用字符串。
3) 数值轴用 type:'value'；类别轴 type:'category' 并提供 data 数组；时间轴 type:'time'。
4) 配色统一用 ECharts 默认调色板（#5470c6 #91cc75 #ee6666 #73a0fa #fac858 #3ba272 等）；用户没指定时不要硬编色名。
5) 标题/副标题/网格/tooltip/legend 按需配置；不输出你无法控制的字段（CDN URL、外部图片、_placeholder 之类）。
6) 【禁止 JS 函数】option 中**禁止**出现 `function (...) {…}` / `() => {…}` 等函数字面量（JSON 不支持函数，会导致解析失败）：
   - 自定义 tooltip / label / axisLabel 文案 → 用字符串模板，如 `'{b}: {c}'`、`'{a} <br/>{b}: {c}'`、`'{b} ({c}%)'`
   - 自定义节点 / 系列配色 → 用 series 顶层 `color: [...]` 数组（让 ECharts 循环取色），或省略该字段使用默认色
   - 字符串模板占位符：`{a}` 系列名 / `{b}` 类目名或数据名 / `{c}` 数值 / `{d}` 饼图百分比 / `{@xxx}` 指定维度值
7) content 字段填 30-80 字中文文字解释该图表表达的核心信息（最大/最小/趋势/对比），不要复述数据。

【最小可用结构 —— 必须严格按此形状输出，option 里只能放 ECharts 字段，绝对不要把 "content" 写进 option】
{
  "option": {
    "title": {"text": "某月销售额"},
    "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
    "grid": {"left": 40, "right": 20, "top": 30, "bottom": 30, "containLabel": true},
    "xAxis": {"type": "category", "data": ["1月", "2月", "3月"]},
    "yAxis": {"type": "value"},
    "series": [
      {"name": "销售额", "type": "bar", "data": [120, 132, 101], "itemStyle": {"borderRadius": [4, 4, 0, 0]}}
    ]
  },
  "content": "3月销售额最低 101，2月最高 132，整体在 100-140 之间波动。"
}

用户给的需求、数据统计摘要、图表类型专属 KB 都在下方用户消息里，按它们生成。"""


# 主生成阶段的结构化输出 schema。
# - option：ECharts option 整体是结构自由的（字段 200+），用 additionalProperties: true 兜住；
#   真正能强约束的是 content 字段。
# - content：30-80 字中文文字解释。
# OpenAI 严格模式要求 additionalProperties 在 schema 中显式声明，因此顶层和 option 都要写。
_CHART_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "echarts_chart_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "option": {
                    "type": "object",
                    "description": "完整的 Apache ECharts option 配置对象。",
                    "additionalProperties": True,
                },
                "content": {
                    "type": "string",
                    "description": "30-80 字中文文字解释该图表表达的核心信息。",
                    "minLength": 1,
                },
            },
            "required": ["option", "content"],
            "additionalProperties": False,
        },
    },
}


_RE_JSON_FENCE = re.compile(
    r"```(?:json|javascript|js|echarts)?\s*(\{[\s\S]+?\})\s*```", re.IGNORECASE
)


def _strip_json_fence(raw: str) -> tuple[Optional[str], str]:
    """从 raw 中抠出 ```json...``` 围栏内的 JSON 切片 + 围栏外的剩余文字。

    Returns:
        (json_inside, text_outside)
        - 找不到围栏：返回 (None, raw)
        - 找到：返回 (围栏内 JSON 字符串, 围栏外剩余文字 trim 后)
    """
    m = _RE_JSON_FENCE.search(raw)
    if not m:
        return None, raw
    inside = m.group(1)
    # 围栏外：raw 里 m.start() 之前 + m.end() 之后
    outside = (raw[: m.start()] + raw[m.end() :]).strip()
    return inside, outside


def _parse_structured_chart(raw: str) -> tuple[Optional[dict], Optional[str], Optional[str], str]:
    """从 LLM 单次返回里直接抠出 (option, content, method, error)。

    主路径：上游 ``response_format=json_schema/json_object`` 强制单层 JSON → ``json.loads`` 一次即得。
    兜底：极少数 LLM/网关不响应 response_format、模型继续用 `` ```json...``` `` 围栏 + 围栏外文字
    —— 这种情况下也认：扫到围栏就 strip，里面若已是 {option, content} 直接用；
    若只是 ECharts option（series/title/...），就把它当 option，围栏外文字当 content。

    Returns:
        (option, content, method, error)
        - 成功：error 为 ``""``
        - 失败：option / content / method 为 ``None``，error 描述问题
        - method 取值：``"primary"`` / ``"in_option"`` / ``"fence_fence_full"`` /
          ``"fence_fence_option"`` / ``"fence_fence_in_option"``，前端用来显示"LLM 偏离"徽章
    """
    if not raw or not raw.strip():
        return None, None, None, "LLM 返回为空"

    # 把 ``<think>...</think>`` 块（Ollama / OpenAI 推理模式 + 我们的拼接）从头部剥掉，
    # 不让它的内容干扰 JSON 解析
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

    def _validate(obj) -> tuple[Optional[dict], Optional[str]]:
        """校验 {option: dict, content: str} 结构。返回 (option, content) 或 (None, None)。"""
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

    def _extract_option_and_content(obj: dict) -> tuple[Optional[dict], Optional[str], bool]:
        """从「裸 ECharts option」里识别/抽离出 content 字段。返回 (option, content, popped)。"""
        if not _is_echarts_option_shape(obj):
            return obj, "", False
        content_val = obj.get("content")
        if isinstance(content_val, str) and content_val.strip():
            new_option = {k: v for k, v in obj.items() if k != "content"}
            return new_option, content_val, True
        return obj, "", False

    # 1) 主路径：直接 json.loads
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if obj is not None:
        got = _validate(obj)
        if got[0] is not None and got[1] is not None:
            return got[0], got[1], "primary", ""
        # 顶层是合法 JSON 但 schema 不匹配 —— 看看是不是 ECharts option 裸对象
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
            # 围栏里只是 ECharts option（没有 option/content 包装）→ 整段当 option，
            # 围栏外文字当 content
            if _is_echarts_option_shape(inner_obj):
                opt2, cnt2, popped = _extract_option_and_content(inner_obj)
                # 围栏外文字作为优先 content（LLM 老格式：option 在围栏里，解释在围栏外）
                if outside.strip():
                    return opt2, outside, "fence_option", ""
                return opt2, cnt2, ("fence_in_option" if popped else "fence_option"), ""

    preview = raw.strip().replace("\n", " ")[:160]
    return None, None, None, f"模型输出不符合结构化 schema：JSON.loads 失败或字段缺失（reply: {preview!r}）"


# 推理深度合法取值。空串 / off / low / medium / high —— "off" 与空串都视为"不发送"。
_LLM_THINKING_ALLOWED = {"", "off", "low", "medium", "high"}


def _normalize_thinking(v: object) -> str:
    """把用户/DB 里的 llm_thinking 值归一化成合法取值。"""
    s = str(v or "").strip().lower()
    return s if s in _LLM_THINKING_ALLOWED else ""


def build_llm_cfg() -> dict:
    cfg = get_all_config()
    thinking = _normalize_thinking(cfg.get("llm_thinking"))
    base_url = cfg.get("llm_base_url", "").strip().rstrip("/")
    return {
        "base_url": base_url,
        "api_key": cfg.get("llm_api_key", "").strip(),
        "model": cfg.get("llm_model", "").strip(),
        "system_prompt": cfg.get("system_prompt", "").strip(),
        "temperature": float(cfg.get("llm_temperature") or 0.7),
        "max_tokens": int(cfg.get("llm_max_tokens") or 2048),
        # 推理深度："" / "off" 不发送；"low"/"medium"/"high" 透传给服务端
        # —— 实际是否会发送还看 provider（Ollama 下 ""→"none" 显式关闭）
        "reasoning_effort": "" if thinking in ("", "off") else thinking,
        # Provider 嗅探：Ollama 的 reasoning_effort 语义与 OpenAI 略有差异
        # —— "off" 在 Ollama 上要发 "none" 才会真正关闭思考
        "provider": llm_client._detect_provider(base_url),
    }


# ---------------------- Routes ----------------------
@app.route("/")
def index():
    return render_template("chat.html")


@app.route("/config")
def config_page():
    """兼容 /config 直链：渲染 chat.html，模态框会在前端自动打开。"""
    return render_template("chat.html", config_open=True)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """读取配置。API Key 不返回明文，只返回「已配置」标志与掩码（例如 sk-***abcd）。"""
    cfg = get_all_config()
    raw_key = cfg.pop("llm_api_key", "") or ""
    if raw_key:
        cfg["llm_api_key_present"] = True
        cfg["llm_api_key_masked"] = (
            raw_key[:3] + "***" + raw_key[-4:] if len(raw_key) > 8 else "***"
        )
    else:
        cfg["llm_api_key_present"] = False
        cfg["llm_api_key_masked"] = ""
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """保存配置。规则：
    - `llm_api_key` 字段如果非空字符串则更新；为 null / 空 / 缺失则保持原值（避免误删）。
    - 其它字段按 value 覆盖。
    """
    data = request.get_json(force=True) or {}
    api_key_in = data.get("llm_api_key")
    for k in (
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "system_prompt",
        "llm_temperature",
        "llm_max_tokens",
        "llm_thinking",
    ):
        if k not in data or data[k] is None:
            continue
        v = data[k]
        if k == "llm_api_key":
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
        elif k == "llm_thinking":
            # 推理深度：必须落在白名单里；非法值原样存（前端下拉会兜底）
            v = str(v).strip()
        set_config(k, str(v))
    return jsonify({"ok": True, "saved_keys": list(data.keys())})


@app.route("/api/config/test", methods=["POST"])
def api_config_test():
    """测试当前 LLM 配置。

    行为：
    - 默认从 DB 读取配置。
    - 接受可选 JSON body，可以**用当前表单值覆盖 DB**，让「先测试再保存」也能用。
    - 如果 body 中没有 api_key，则用 DB 里的 key（保留之前设置的值）。
    """
    body = request.get_json(silent=True) or {}
    cfg = build_llm_cfg()
    if body.get("llm_base_url"):
        cfg["base_url"] = str(body["llm_base_url"]).strip().rstrip("/")
    if body.get("llm_api_key"):
        cfg["api_key"] = str(body["llm_api_key"]).strip()
    if body.get("llm_model"):
        cfg["model"] = str(body["llm_model"]).strip()
    if "llm_temperature" in body and body["llm_temperature"] is not None:
        try:
            cfg["temperature"] = float(body["llm_temperature"])
        except (TypeError, ValueError):
            pass
    if "llm_max_tokens" in body and body["llm_max_tokens"] is not None:
        try:
            cfg["max_tokens"] = int(body["llm_max_tokens"])
        except (TypeError, ValueError):
            pass
    if "llm_thinking" in body and body["llm_thinking"] is not None:
        cfg["reasoning_effort"] = "" if _normalize_thinking(body["llm_thinking"]) in ("", "off") else _normalize_thinking(body["llm_thinking"])

    if not cfg["api_key"]:
        return jsonify({"ok": False, "message": "未填写 API Key（请在表单填入或先保存一次）"}), 400
    try:
        reply = call_llm(
            cfg,
            messages=[
                {"role": "system", "content": "你是一个简短的助手。"},
                {"role": "user", "content": "请仅回复一个单词：OK"},
            ],
            max_tokens=20,
        )
        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """解析用户上传的文件或直接粘贴的数据文本。

    支持两种用法：
    - 纯代码解析（默认）：走 pandas / csv / json，返回 {columns, rows, ...}。
    - LLM 智能整理：当客户端传 `use_llm=1`（form 或 query）时，会在代码解析
      结果上再调用大模型做语义理解、列名规范化、类型推断、剔除噪声行等。
      要求 LLM 配置已填写完成。
    """
    file = request.files.get("file")
    text = request.form.get("text") or _get_param("text")

    use_llm = parse_bool(_get_param("use_llm"))
    no_header = parse_bool(_get_param("no_header"))
    user_hint = _get_param("hint") or ""

    # 用户可能多 sheet xlsx 选完后再次提交
    selected_sheets = request.form.getlist("selected_sheets") or None
    if not selected_sheets:
        # JSON body 路径
        ss = (request.get_json(silent=True) or {}).get("selected_sheets")
        if isinstance(ss, list):
            selected_sheets = [str(x) for x in ss if x]

    try:
        if file:
            filename = (file.filename or "").lower()
            ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

            # xlsx/xls + 还没选 sheet：列出所有 sheet 让用户选
            if ext in ("xlsx", "xls") and not selected_sheets:
                try:
                    file.stream.seek(0)
                except Exception:
                    pass
                raw = file.read()
                sheets = list_excel_sheets(raw)
                if len(sheets) > 1:
                    return jsonify({
                        "needs_sheet_selection": True,
                        "sheets": sheets,
                        "source_ext": ext,
                        "no_header": no_header,
                    })
                # 单 sheet：直接走 parse_upload
                wrapped = FileStorage(stream=io.BytesIO(raw), filename=file.filename)
                parsed = parse_upload(wrapped, no_header=no_header, selected_sheets=None)
            else:
                parsed = parse_upload(file, no_header=no_header, selected_sheets=selected_sheets)
        elif text:
            parsed = parse_data_text(text, no_header=no_header)
        else:
            return jsonify({"error": "请上传文件或粘贴数据文本。"}), 400
    except Exception as e:
        return jsonify({"error": f"数据解析失败：{e}"}), 400

    if use_llm:
        api_key = get_config("llm_api_key")
        if not api_key:
            return jsonify({
                "error": "已请求大模型整理，但缺少 LLM 配置。请先前往「配置」页填写。",
                "need_config": True,
                "fallback": parsed,
            }), 428
        cfg = build_llm_cfg()
        result = understand_data(cfg, parsed.get("raw_text", ""), parsed, user_hint=user_hint)
        # 保留原始代码解析结果作为对照
        result["code_parsed"] = {
            "columns": parsed.get("columns", []),
            "rows": parsed.get("rows", []),
            "count": parsed.get("count", 0),
            "description": parsed.get("description", ""),
        }
        return jsonify(result)

    return jsonify(parsed)


@app.route("/api/knowledge", methods=["GET"])
def api_knowledge():
    q = request.args.get("q", "").strip()
    chart_type = request.args.get("chart_type", "").strip()
    if chart_type:
        result = get_knowledge_for_type(chart_type)
    else:
        result = search_knowledge(q)
    return jsonify(result)


@app.route("/api/chart", methods=["POST"])
@config_required
def api_chart():
    """非流式 JSON 接口（保留旧行为）：走完整 pipeline，但只返回最终结果。"""
    prompt, data, chart_type_hint, style_hint = _parse_chart_request()

    cfg = build_llm_cfg()
    final = None
    error_payload = None
    for evt in run_chart_pipeline(cfg, prompt, data, chart_type_hint, style_hint, stream=False):
        kind = evt.get("type")
        if kind == "done":
            final = {k: v for k, v in evt.items() if k != "type"}
        elif kind == "error":
            error_payload = evt
            break

    if error_payload is not None:
        msg = error_payload.get("message") or "生成失败"
        status = error_payload.get("status", 500)
        payload = {"error": msg}
        if error_payload.get("raw_reply"):
            payload["raw_reply"] = error_payload["raw_reply"]
        return jsonify(payload), status

    if final is None:
        return jsonify({"error": "生成失败"}), 500
    return jsonify(final)


def _parse_chart_request() -> tuple[str, object, str, dict]:
    """从 request body 抽出 ``(prompt, data, chart_type_hint, style_hint)``。

    无 prompt / 无 data 时直接 400 中断（通过 ``abort``）。
    """
    body = request.get_json(force=True) or {}
    prompt = (body.get("prompt") or "").strip()
    data = body.get("data")
    chart_type_hint = (body.get("chart_type_hint") or "").strip()
    style_hint = body.get("style_hint") or {}
    if not prompt and not data:
        abort(400, description="请至少输入需求或提供数据。")
    return prompt, data, chart_type_hint, style_hint


@app.route("/api/chart/stream", methods=["POST"])
@config_required
def api_chart_stream():
    """流式（SSE）接口：边生成边把阶段事件与模型输出 token 推给前端。

    - 生成器全程用 ``yield`` 推送，每产生一个事件立即 flush，避免被中间代理/
      服务器缓冲；
    - 内置心跳（heartbeat）事件：每 8 秒若还没有真实事件产出，则发一个空
      ``: heartbeat`` 注释事件，让代理与浏览器知道连接仍活跃；
    - 所有异常都被包住 → 以 ``{"type":"error"}`` 事件正常结束流，而不是
      让底层抛出异常把 TCP 连接打断，避免前端报 "读取流失败"。
    """
    prompt, data, chart_type_hint, style_hint = _parse_chart_request()
    cfg = build_llm_cfg()

    # 共享状态：pipeline_events 队列 + 终止信号
    pipeline_events: list = []
    finished = threading.Event()
    errored = threading.Event()
    lock = threading.Lock()

    def producer():
        try:
            for evt in run_chart_pipeline(
                cfg, prompt, data, chart_type_hint, style_hint, stream=True
            ):
                with lock:
                    pipeline_events.append(evt)
                # 让消费者尽快看到（不做全局 sleep 也行，消费者里有短轮询）
        except Exception as e:
            with lock:
                pipeline_events.append(
                    {"type": "error", "message": f"生成失败：{e}"}
                )
            errored.set()
        finally:
            finished.set()

    # 后台线程跑 pipeline，主线程负责 yield 给 Flask
    t = threading.Thread(target=producer, daemon=True)
    t.start()

    def generate():
        last_evt_at = time.time()
        sent_done_or_error = False
        # 先推一条空事件，让中间代理/浏览器建立流状态
        yield ": ok\n\n"
        try:
            while True:
                # 1) 先把所有已产出事件立刻推出去
                while True:
                    with lock:
                        if not pipeline_events:
                            break
                        evt = pipeline_events.pop(0)
                    yield _sse_format_event(evt)
                    last_evt_at = time.time()
                    if evt.get("type") in ("done", "error"):
                        sent_done_or_error = True
                        break

                if sent_done_or_error:
                    break

                # 2) pipeline 已自然结束但没产出 done / error → 兜底
                if finished.is_set():
                    with lock:
                        remaining = list(pipeline_events)
                        pipeline_events.clear()
                    for evt in remaining:
                        yield _sse_format_event(evt)
                        if evt.get("type") in ("done", "error"):
                            sent_done_or_error = True
                            break
                    if not sent_done_or_error:
                        yield _sse_format_event(
                            {"type": "error", "message": "生成失败：服务端异常中断"}
                        )
                    break

                # 3) 心跳：超过 8 秒没有任何事件，发一行注释保持连接
                now = time.time()
                if now - last_evt_at > 8:
                    yield ": heartbeat\n\n"
                    last_evt_at = now
                    continue

                # 4) 短轮询：避免 busy loop，又不至于延迟太大
                time.sleep(0.05)
        finally:
            # 确保后台线程被等一下（它已经 daemon，进程退出也会被回收）
            pass

    resp = Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Powered-By": "echarts-agent",
        },
    )
    # waitress / Flask: 每一次 yield 都立刻 flush，不要被 WSGI 缓冲
    resp.direct_passthrough = True
    return resp


def _sse_format_event(obj: dict) -> str:
    """把一个事件 dict 格式化为 SSE 帧：``data: {json}\\n\\n``。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ---------------------- Chart Generation Pipeline ----------------------
def run_chart_pipeline(
    cfg: dict,
    prompt: str,
    data,
    chart_type_hint: str,
    style_hint,
    *,
    stream: bool = False,
):
    """统一的图表生成 pipeline，按阶段产出事件。

    Yields:
        dict: 事件对象，``type`` 字段取值：
            - ``stage``：阶段状态变更（``status`` 为 ``start`` / ``done`` / ``error``）
            - ``delta``：模型流式输出（``stream=True`` 时才有；``content`` 是单次 chunk）
            - ``chart_type``：图表类型已确定（仅当 ``stage=pick_type`` 结束时）
            - ``understanding``：LLM 整理结果摘要（仅当 ``stage=understand`` 结束时）
            - ``done``：最终结果（带 ``chart_type`` / ``option`` / ``code`` / ``raw_reply`` 等）
            - ``error``：错误（带 ``message`` / ``status`` / ``raw_reply`` 可选）

    ``stream=False`` 时，主生成阶段退化为 ``call_llm`` 一次性调用，不产出 ``delta`` 事件。
    """
    # ---- 1) 数据准备（瞬时） ----
    yield {"type": "stage", "stage": "prepare", "label": "数据准备", "status": "start"}
    if not prompt and not data:
        yield {"type": "error", "message": "请至少输入需求或提供数据。", "status": 400}
        return
    yield {"type": "stage", "stage": "prepare", "status": "done"}

    # ---- 2) 智能数据整理 ----
    # 触发条件（按优先级）：
    #   a) 数据已经在 /api/parse 阶段被 LLM 整理过（用户在解析时勾了 🧠）—— 直接复用
    #   b) 本次 /api/chart 请求里显式带 need_understanding=true（用户在生成阶段勾了 🧠）—— 跑一次
    #   c) 都没有 —— 跳过
    yield {"type": "stage", "stage": "understand", "label": "智能数据整理", "status": "start"}
    understanding = None
    existing_method = (data or {}).get("understand_method")
    need_understanding = bool(data and parse_bool(data.get("need_understanding")))

    if existing_method in ("llm", "fallback"):
        # 情况 (a)：解析阶段已整理；这里把结果同步进 pipeline 变量（data 已经是整理后的）
        understanding = {
            "method": existing_method,
            "summary": (data or {}).get("summary", "") or "",
            "notes": (data or {}).get("notes", "") or "",
            "error": (data or {}).get("understand_error"),
            "reused": True,  # 标记：结果是从 /api/parse 阶段复用，没有重跑
        }
        yield {
            "type": "stage",
            "stage": "understand",
            "status": "done",
            "understanding": understanding,
        }
    elif need_understanding:
        # 情况 (b)：本次请求显式要求重新整理
        try:
            result = understand_data(
                cfg,
                (data or {}).get("raw_text", ""),
                data,
                prompt,
            )
            data = result
            understanding = {
                "method": result.get("understand_method"),
                "summary": result.get("summary", ""),
                "notes": result.get("notes", ""),
                "error": result.get("understand_error"),
            }
        except Exception as e:
            understanding = {
                "method": "fallback",
                "summary": "",
                "notes": "",
                "error": f"{e}",
            }
        yield {
            "type": "stage",
            "stage": "understand",
            "status": "done",
            "understanding": understanding,
        }
    else:
        # 情况 (c)：未启用（数据是代码解析结果）
        understanding = {"method": "skipped", "summary": "", "notes": "", "error": None}
        yield {
            "type": "stage",
            "stage": "understand",
            "status": "skipped",
            "understanding": understanding,
        }

    # ---- 3) 数据预处理（基于用户 prompt 自动应用规则） ----
    yield {"type": "stage", "stage": "preprocess", "label": "数据预处理", "status": "start"}
    preprocess_info = None
    if data and data.get("rows"):
        try:
            new_data, pp_info = preprocess_data(prompt or "", data)
            preprocess_info = pp_info
            if pp_info.get("rules"):
                # 真正识别到规则才把数据替换掉
                data = new_data
        except Exception as e:
            preprocess_info = {
                "rules": [],
                "applied": [],
                "skipped": [],
                "summary": f"预处理异常：{e}",
            }
    else:
        preprocess_info = {
            "rules": [],
            "applied": [],
            "skipped": [],
            "summary": "无可预处理的数据",
        }
    has_applied = bool(preprocess_info and preprocess_info.get("applied"))
    yield {
        "type": "stage",
        "stage": "preprocess",
        "status": "done" if has_applied else "skipped",
        "preprocess": preprocess_info,
    }

    # ---- 4) 选择图表类型 ----
    yield {"type": "stage", "stage": "pick_type", "label": "选择图表类型", "status": "start"}
    chosen_type, type_reason = None, None
    try:
        chosen_type, type_reason = pick_chart_type(cfg, prompt, data, chart_type_hint)
    except Exception as e:
        chosen_type, type_reason = "bar", f"LLM 调用失败，默认使用柱状图：{e}"
    if not chosen_type:
        chosen_type, type_reason = "bar", "未能推荐图表类型，默认使用柱状图。"
    yield {
        "type": "stage",
        "stage": "pick_type",
        "status": "done",
        "chart_type": chosen_type,
        "reason": type_reason,
    }

    # ---- 5) 检索知识库 + 构造 prompt ----
    kb = get_knowledge_for_type(chosen_type)
    gen_prompt = build_chart_prompt(prompt, data, chosen_type, style_hint, kb)

    # 把预处理结果作为附加上下文告诉 LLM，让它知道数据已经被改过，
    # 这样它能正确选择 tooltip / axisLabel 的精度（如 .toFixed(2)）。
    if has_applied:
        actions = [
            a.get("action")
            for a in preprocess_info.get("applied", [])
            if a.get("action") and not a.get("skipped")
        ]
        actions = [a for a in actions if a]
        if actions:
            gen_prompt += (
                "\n\n【数据预处理已应用】以下规则已对数据生效（前端已按结果展示，"
                "请在 ECharts option 的 tooltip / axisLabel / series.label 等展示处使用与数据一致的精度"
                "（例如 toFixed(2)），避免出现 1.2300000001 之类的尾数）：\n"
                + "\n".join(f"- {a}" for a in actions)
            )

    sys_prompt = cfg["system_prompt"] or _DEFAULT_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": gen_prompt},
    ]

    # ---- 6) 主生成：调用 LLM（流式或一次性） ----
    yield {"type": "stage", "stage": "generate", "label": "生成图表配置", "status": "start"}
    raw = ""
    try:
        if stream:
            # 用内层 try-catch 确保流式调用中出现异常时不会中断整个生成器
            # 任何在流式调用中抛的异常都会在这里捕获，yield 错误事件然后 return
            stream_iter = call_llm_stream(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=cfg["temperature"],
                response_format=_CHART_RESPONSE_SCHEMA,
                reasoning_effort=cfg.get("reasoning_effort") or None,
            )
            while True:
                try:
                    chunk = next(stream_iter)
                except StopIteration:
                    break
                except Exception as inner_e:
                    # LLM 流式调用中途失败 —— 转化为错误事件
                    yield {
                        "type": "stage",
                        "stage": "generate",
                        "status": "error",
                        "message": str(inner_e),
                    }
                    yield {
                        "type": "error",
                        "message": f"模型流式调用失败：{inner_e}",
                        "raw_reply": raw,
                        "status": 500,
                    }
                    return
                raw += chunk
                if chunk:
                    yield {"type": "delta", "content": chunk}
            reasoning = ""
        else:
            raw, reasoning = call_llm_raw(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=cfg["temperature"],
                response_format=_CHART_RESPONSE_SCHEMA,
                reasoning_effort=cfg.get("reasoning_effort") or None,
            )
            if reasoning:
                raw = f"<think>\n{reasoning}\n</think>\n\n{raw}"
    except Exception as e:
        yield {
            "type": "stage",
            "stage": "generate",
            "status": "error",
            "message": str(e),
        }
        yield {"type": "error", "message": f"模型调用失败：{e}", "raw_reply": raw, "status": 500}
        return
    yield {"type": "stage", "stage": "generate", "status": "done", "length": len(raw)}

    # ---- 7) 解析（结构化输出 → 一次 json.loads 即够） ----
    yield {"type": "stage", "stage": "parse", "label": "解析与校验", "status": "start"}
    option, content, parse_method, parse_error = _parse_structured_chart(raw)
    if parse_error:
        yield {
            "type": "stage",
            "stage": "parse",
            "status": "error",
            "message": parse_error,
        }
        yield {
            "type": "error",
            "message": f"模型输出不符合结构化 schema：{parse_error}",
            "raw_reply": raw,
            "status": 502,
        }
        return
    assert option is not None and content is not None
    code = None
    chart_js = wrap_code(option, code, chosen_type)

    # ---- 8) 保存到默认会话 ----
    chat_id = ensure_default_chat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages(chat_id,role,content,data_json,option_json,code,chart_type,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                chat_id,
                "assistant",
                content or raw,
                json.dumps(data, ensure_ascii=False) if data else None,
                json.dumps(option, ensure_ascii=False),
                chart_js,
                chosen_type,
                now_iso(),
            ),
        )
        conn.commit()

    yield {"type": "stage", "stage": "parse", "status": "done"}

    resp = {
        "chart_type": chosen_type,
        "type_reason": type_reason,
        "option": option,
        "code": chart_js,
        "content": content,
        # 兼容字段：旧前端 / 旧消息会读 explanation
        "explanation": content,
        "raw_reply": raw,
        # primary / in_option / fence_full / fence_option / fence_in_option —— 前端用
        # 来判断是否提示用户「LLM 没按 schema 输出」
        "parse_method": parse_method or "primary",
    }
    if understanding is not None:
        resp["understanding"] = understanding
    if preprocess_info is not None:
        resp["preprocess"] = preprocess_info
    yield {"type": "done", **resp}


# ---------------------- Chat History ----------------------
def ensure_default_chat() -> str:
    default_id = "default"
    with get_db() as conn:
        row = conn.execute("SELECT id FROM chats WHERE id = ?", (default_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO chats(id, title, created_at, updated_at) VALUES(?,?,?,?)",
                (default_id, "默认对话", now_iso(), now_iso()),
            )
            conn.commit()
    return default_id


@app.route("/api/chats", methods=["GET"])
def api_chats():
    with get_db() as conn:
        rows = conn.execute("SELECT id, title, updated_at FROM chats ORDER BY updated_at DESC").fetchall()
        return jsonify([dict(r) for r in rows])


@app.route("/api/chats/<chat_id>/messages", methods=["GET"])
def api_chat_messages(chat_id: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, chart_type, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])



def wrap_code(option: dict, llm_code: Optional[str], chart_type: str) -> str:
    """
    生成一段可在前端 chart 容器内直接运行的 JavaScript 代码。
    用户前端只需：
      const chart = echarts.init(document.getElementById('chart'));
      eval(返回代码);  // 或解析后 chart.setOption(option)
    这里返回一份包含 option 与 setOption 的完整代码字符串。
    """
    if llm_code and "setOption" in llm_code:
        return llm_code
    option_js = json.dumps(option, ensure_ascii=False, indent=2)
    return (
        f"// 图表类型：{chart_type}\n"
        f"const option = {option_js};\n"
        f"if (typeof chart !== 'undefined') chart.setOption(option);\n"
    )


# ---------------------- Main ----------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[ECharts Agent] starting on http://{host}:{port}")

    # 优先用 waitress（生产级 WSGI，流式响应不会被开发服务器缓冲）；
    # 若 waitress 未安装则退回 Flask 开发服务器 + threaded=True，确保流式能跑。
    try:
        from waitress import serve

        print(f"[ECharts Agent] using waitress (WSGI)")
        serve(
            app,
            host=host,
            port=port,
            threads=8,
            # waitress 内部会把 write() 立刻 flush，不用额外 backlog 设置
            channel_timeout=300,  # 允许一次请求最多跑 5 分钟（大模型流式）
        )
    except Exception as e:
        print(f"[ECharts Agent] waitress 不可用（{e}），退回 Flask 开发服务器")
        app.run(host=host, port=port, debug=False, threaded=True)
