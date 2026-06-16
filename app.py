#!/usr/bin/env python3
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from flask_cors import CORS
from werkzeug.datastructures import FileStorage

from llm_client import call_llm, call_llm_stream, build_chart_prompt, pick_chart_type
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

【硬约束】
1) 只输出一个 JSON 对象，用 ```json ... ``` 包裹；JSON 内不要写注释、不要 markdown 链接。
2) 严格使用用户给的数据，不编造、不替换；xAxis/yAxis 的 data 与列名一致；数值不要用字符串。
3) 数值轴用 type:'value'；类别轴 type:'category' 并提供 data 数组；时间轴 type:'time'。
4) 配色统一用 ECharts 默认调色板（#5470c6 #91cc75 #ee6666 #73a0fa #fac858 #3ba272 等）；用户没指定时不要硬编色名。
5) 标题/副标题/网格/tooltip/legend 按需配置；不输出你无法控制的字段（CDN URL、外部图片、_placeholder 之类）。
6) JSON 之后用 30-80 字中文简要说明该图表达的核心信息（最大/最小/趋势/对比），不要复述数据。
7) 【禁止 JS 函数】option 中**禁止**出现 `function (...) {…}` / `() => {…}` 等函数字面量（JSON 不支持函数，会导致解析失败）：
   - 自定义 tooltip / label / axisLabel 文案 → 用字符串模板，如 `'{b}: {c}'`、`'{a} <br/>{b}: {c}'`、`'{b} ({c}%)'`
   - 自定义节点 / 系列配色 → 用 series 顶层 `color: [...]` 数组（让 ECharts 循环取色），或省略该字段使用默认色
   - 字符串模板占位符：`{a}` 系列名 / `{b}` 类目名或数据名 / `{c}` 数值 / `{d}` 饼图百分比 / `{@xxx}` 指定维度值

【最小可用结构（bar / line 参考）】
```json
{
  "title": {"text": "某月销售额"},
  "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
  "grid": {"left": 40, "right": 20, "top": 30, "bottom": 30, "containLabel": true},
  "xAxis": {"type": "category", "data": ["1月", "2月", "3月"]},
  "yAxis": {"type": "value"},
  "series": [
    {"name": "销售额", "type": "bar", "data": [120, 132, 101], "itemStyle": {"borderRadius": [4, 4, 0, 0]}}
  ]
}
```

用户给的需求、数据统计摘要、图表类型专属 KB 都在下方用户消息里，按它们生成。"""


def build_llm_cfg() -> dict:
    cfg = get_all_config()
    return {
        "base_url": cfg.get("llm_base_url", "").strip().rstrip("/"),
        "api_key": cfg.get("llm_api_key", "").strip(),
        "model": cfg.get("llm_model", "").strip(),
        "system_prompt": cfg.get("system_prompt", "").strip(),
        "temperature": float(cfg.get("llm_temperature") or 0.7),
        "max_tokens": int(cfg.get("llm_max_tokens") or 2048),
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
    ):
        if k not in data or data[k] is None:
            continue
        v = data[k]
        if k == "llm_api_key":
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
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
    body = request.get_json(force=True) or {}
    prompt = (body.get("prompt") or "").strip()
    data = body.get("data")
    chart_type_hint = body.get("chart_type_hint") or ""
    style_hint = body.get("style_hint") or {}

    if not prompt and not data:
        return jsonify({"error": "请至少输入需求或提供数据。"}), 400

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


@app.route("/api/chart/stream", methods=["POST"])
@config_required
def api_chart_stream():
    """流式（SSE）接口：边生成边把阶段事件与模型输出 token 推给前端。

    协议：``text/event-stream``，每条事件一行::

        data: {"type":"stage", ...}\\n\\n
        data: {"type":"delta","content":"..."}\\n\\n
        data: {"type":"done", "chart_type":..., "option":..., ...}\\n\\n

    失败事件::

        data: {"type":"error", "message":"..."}\\n\\n
    """
    body = request.get_json(force=True) or {}
    prompt = (body.get("prompt") or "").strip()
    data = body.get("data")
    chart_type_hint = body.get("chart_type_hint") or ""
    style_hint = body.get("style_hint") or {}

    if not prompt and not data:
        return jsonify({"error": "请至少输入需求或提供数据。"}), 400

    cfg = build_llm_cfg()

    def _sse_format(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def generate():
        try:
            for evt in run_chart_pipeline(cfg, prompt, data, chart_type_hint, style_hint, stream=True):
                yield _sse_format(evt)
        except Exception as e:
            # 兜底：pipeline 自己已经在出错位置 emit 过 error；这里再补一发以防外层异常
            yield _sse_format({"type": "error", "message": f"生成失败：{e}"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 防止 nginx 等代理缓冲
            "Connection": "keep-alive",
        },
    )


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
            for chunk in call_llm_stream(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=cfg["temperature"],
            ):
                raw += chunk
                if chunk:
                    yield {"type": "delta", "content": chunk}
        else:
            raw = call_llm(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=cfg["temperature"],
            )
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

    # ---- 7) 解析 + 自动重试 ----
    yield {"type": "stage", "stage": "parse", "label": "解析与校验", "status": "start"}
    parsed = extract_json(raw)
    retried = False
    if parsed is None:
        # 一次自动重试
        yield {"type": "stage", "stage": "parse", "substep": "retry", "status": "start"}
        retry_outcome = yield from _retry_parse_json(cfg, sys_prompt, raw, stream=stream)
        # _retry_parse_json 是个生成器，每个元素是事件；最后一次通过 return 传结果
        new_raw, fixed, error_event = retry_outcome
        if fixed is not None:
            raw = new_raw
            parsed = fixed
            retried = True
            yield {"type": "stage", "stage": "parse", "substep": "retry", "status": "done"}
        else:
            # 把子生成器最后的错误事件原样转发
            if error_event is not None:
                yield error_event
            else:
                yield {
                    "type": "stage",
                    "stage": "parse",
                    "status": "error",
                    "message": "无法从模型回复中解析出 JSON（已尝试自动修正）",
                }
                yield {
                    "type": "error",
                    "message": "无法从模型回复中解析出 JSON 配置（已尝试一次自动修正，仍未通过）。",
                    "raw_reply": raw,
                    "status": 502,
                }
            return

    option, code, explanation, fn_map = parsed
    chart_js = wrap_code(option, code or "", chosen_type, fn_map=fn_map)

    # ---- 8) 保存到默认会话 ----
    chat_id = ensure_default_chat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages(chat_id,role,content,data_json,option_json,code,chart_type,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                chat_id,
                "assistant",
                explanation or raw,
                json.dumps(data, ensure_ascii=False) if data else None,
                json.dumps(option, ensure_ascii=False),
                chart_js,
                chosen_type,
                now_iso(),
            ),
        )
        conn.commit()

    yield {"type": "stage", "stage": "parse", "status": "done", "retried": retried}

    resp = {
        "chart_type": chosen_type,
        "type_reason": type_reason,
        "option": option,
        "code": chart_js,
        "explanation": explanation,
        "raw_reply": raw,
        "retried": retried,
        "fn_map": fn_map or None,
    }
    if understanding is not None:
        resp["understanding"] = understanding
    if preprocess_info is not None:
        resp["preprocess"] = preprocess_info
    yield {"type": "done", **resp}


def _build_retry_prompt(broken_raw: str) -> str:
    """构造让 LLM 修正 JSON 输出的 prompt。"""
    return (
        "你上一轮的回复无法被解析为合法 JSON（常见原因：漏了闭合括号、"
        "JSON 末尾多了文本或表格、混入了多余代码块、行尾有未闭合的 // 注释、"
        "**option 中写了 JS 函数字面量**）。\n"
        f"你上一轮的原始回复（前 4000 字符）：\n```\n{broken_raw[:4000]}\n```\n\n"
        "请基于同一份需求重新输出，**严格遵守**：\n"
        "1) 只输出一个用 ```json ... ``` 包裹的合法 JSON 对象；\n"
        "2) JSON 内不要写任何注释（// 或 /* */）；\n"
        "3) 不要输出多余的解释文字、表格或第二个代码块；\n"
        "4) 确保所有大括号、中括号、双引号都正确闭合；\n"
        "5) 【关键】option 中**禁止 JS 函数**（`function () {}` / `() => {}`）—— JSON 不支持函数，"
        "解析必失败：`formatter` 用字符串模板（如 `'{b}: {c}'`），`color` 用 series 顶层 `color: [...]` 数组或省略。"
    )


def _retry_parse_json(cfg: dict, sys_prompt: str, broken_raw: str, *, stream: bool):
    """让 LLM 重新生成一次，修复坏掉的 JSON 输出。

    同时作为流式事件源：过程中 yield  ``delta`` 事件（如果 stream=True）。
    最终通过 ``return (new_raw, parsed_or_None, error_event_or_None)`` 一次性把结果传回调用方。
    """
    fix_msg = _build_retry_prompt(broken_raw)
    temperature = max(0.3, cfg.get("temperature") or 0.7)  # 略偏向确定性，便于稳定
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": fix_msg},
    ]
    new_raw = ""
    try:
        if stream:
            for chunk in call_llm_stream(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=temperature,
            ):
                new_raw += chunk
                if chunk:
                    yield {"type": "delta", "content": chunk}
        else:
            new_raw = call_llm(
                cfg,
                messages=messages,
                max_tokens=cfg["max_tokens"],
                temperature=temperature,
            )
    except Exception as e:
        err = {
            "type": "stage",
            "stage": "parse",
            "status": "error",
            "message": str(e),
        }
        return (new_raw, None, {
            "type": "error",
            "message": f"自动修正失败：{e}",
            "raw_reply": new_raw,
            "status": 500,
        })

    fixed = extract_json(new_raw)
    if fixed is not None:
        return (new_raw, fixed, None)
    return (new_raw, None, {
        "type": "error",
        "message": "无法从模型回复中解析出 JSON 配置（已尝试一次自动修正，仍未通过）。",
        "raw_reply": broken_raw,
        "status": 502,
    })


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


# ---------------------- Helpers ----------------------
# JSON 候选最大长度：超过即放弃（防 LLM 异常输出巨大字符串时拖累 json.loads / re.sub）。
_MAX_JSON_CANDIDATE = 500_000
# 解释文本截断长度。
_MAX_EXPLANATION = 1500

# 预编译热路径正则：抓 JSON 代码块、抓 JS/JS/Python 代码块、去所有代码块。
_RE_JSON_FENCE = re.compile(
    r"```(?:json|echarts|javascript)?\s*(\{[\s\S]+?\})\s*```", re.IGNORECASE
)
_RE_JS_FENCE = re.compile(
    r"```javascript\s*([\s\S]+?)\s*```", re.IGNORECASE
)
_RE_ALT_FENCE = re.compile(
    r"```(?:js|python)\s*([\s\S]+?)\s*```", re.IGNORECASE
)
_RE_ANY_FENCE = re.compile(r"```[\s\S]+?```")
_RE_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_RE_LINE_COMMENT = re.compile(r"//[^\n]*")
_RE_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")


def _scan_json_object(raw: str, max_size: int = _MAX_JSON_CANDIDATE) -> Optional[str]:
    """扫描 raw 中**第一个完整的顶层 JSON 对象**并返回切片。

    关键改进（相对 ``raw.find("{") + raw.rfind("}")``）：
    - 用栈式扫描，严格匹配大括号，正确处理字符串字面量与 ``\\"`` 转义；
    - 找到首个匹配即返回，**不会**把后续解释/噪声一起包进来；
    - 候选长度超过 ``max_size`` 直接放弃（防退化）。
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


def _try_fix_json(text: str) -> str:
    """对 LLM 输出的「几乎对的 JSON」做最小修复：去行/块注释 + 尾随逗号。

    跳过不可能命中的分支（没 ``//`` / ``/*`` 时不跑注释扫描），避免对大字符串白做工。
    不做引号替换（容易误伤）；只处理最常见的两类错。
    """
    if "//" in text or "/*" in text:
        text = _RE_LINE_COMMENT.sub("", text)
        text = _RE_BLOCK_COMMENT.sub("", text)
    return _RE_TRAILING_COMMA.sub(r"\1", text)


# 占位符前缀 + UUID，避免与 option 真实内容冲突；wrap_code 阶段还原成原始 JS 源码。
_FN_PLACEHOLDER_PREFIX = "__ECHARTS_FN_"


def _find_matching_brace(text: str, start: int) -> Optional[int]:
    """在 text[start] 必须是 ``{`` 的前提下，找到与之配对的 ``}`` 索引。

    正确处理：JSON 双引号字符串、JS 单引号字符串、JS 模板字符串（`` `…${ … }…` `` 内插里的嵌套大括号也平衡）、
    行/块注释。配对失败（不闭合）返回 ``None``。
    """
    if start >= len(text) or text[start] != "{":
        return None
    n = len(text)
    depth = 1
    i = start + 1
    in_str = False
    in_squote = False
    in_tmpl = False
    in_line = False
    in_block = False
    while i < n:
        c = text[i]
        if in_line:
            if c == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if in_squote:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_tmpl:
            if c == "\\":
                i += 2
                continue
            if c == "`":
                in_tmpl = False
                i += 1
                continue
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                # 模板字符串内插：单独平衡 ${…} 里的花括号
                i += 2
                sub_depth = 1
                sub_in_str = False
                sub_in_squote = False
                while i < n and sub_depth > 0:
                    cc = text[i]
                    if sub_in_str:
                        if cc == "\\":
                            i += 2
                            continue
                        if cc == '"':
                            sub_in_str = False
                        i += 1
                        continue
                    if sub_in_squote:
                        if cc == "\\":
                            i += 2
                            continue
                        if cc == "'":
                            sub_in_squote = False
                        i += 1
                        continue
                    if cc == '"':
                        sub_in_str = True
                        i += 1
                        continue
                    if cc == "'":
                        sub_in_squote = True
                        i += 1
                        continue
                    if cc == "{":
                        sub_depth += 1
                    elif cc == "}":
                        sub_depth -= 1
                        if sub_depth == 0:
                            i += 1
                            break
                    i += 1
                continue
            i += 1
            continue
        # Normal
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                in_line = True
                i += 2
                continue
            if text[i + 1] == "*":
                in_block = True
                i += 2
                continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "'":
            in_squote = True
            i += 1
            continue
        if c == "`":
            in_tmpl = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _strip_js_functions(candidate: str) -> tuple:
    """从 JSON 风格文本里抠出 ``function (...) {…}`` 字面量，换成唯一占位符字符串。

    LLM 生成的 ECharts option 常带 ``tooltip.formatter`` / ``itemStyle.color`` 等
    JS 函数 —— 这些**不是合法 JSON**，``json.loads`` 直接挂。本函数把函数字面量
    整段换成 ``"__ECHARTS_FN_<uuid>__"``（带引号，作为合法 JSON 字符串），
    让 ``json.loads`` 通过；前端拿到 ``fn_map`` 后再把占位符还原成真函数。

    Returns:
        (cleaned_candidate, {placeholder: original_fn_text})
    """
    n = len(candidate)
    out_parts: list = []
    fn_map: dict = {}
    i = 0
    last = 0

    in_str = False
    in_squote = False
    in_tmpl = False
    in_line = False
    in_block = False

    def is_word(c: str) -> bool:
        return c.isalnum() or c == "_" or c == "$"

    while i < n:
        c = candidate[i]
        if in_line:
            if c == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if c == "*" and i + 1 < n and candidate[i + 1] == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if in_squote:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_tmpl:
            if c == "\\":
                i += 2
                continue
            if c == "`":
                in_tmpl = False
                i += 1
                continue
            if c == "$" and i + 1 < n and candidate[i + 1] == "{":
                # 模板内插：单独平衡
                i += 2
                sub_depth = 1
                sub_in_str = False
                sub_in_squote = False
                while i < n and sub_depth > 0:
                    cc = candidate[i]
                    if sub_in_str:
                        if cc == "\\":
                            i += 2
                            continue
                        if cc == '"':
                            sub_in_str = False
                        i += 1
                        continue
                    if sub_in_squote:
                        if cc == "\\":
                            i += 2
                            continue
                        if cc == "'":
                            sub_in_squote = False
                        i += 1
                        continue
                    if cc == '"':
                        sub_in_str = True
                        i += 1
                        continue
                    if cc == "'":
                        sub_in_squote = True
                        i += 1
                        continue
                    if cc == "{":
                        sub_depth += 1
                    elif cc == "}":
                        sub_depth -= 1
                        if sub_depth == 0:
                            i += 1
                            break
                    i += 1
                continue
            i += 1
            continue
        # Normal
        if c == "/" and i + 1 < n:
            if candidate[i + 1] == "/":
                in_line = True
                i += 2
                continue
            if candidate[i + 1] == "*":
                in_block = True
                i += 2
                continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "'":
            in_squote = True
            i += 1
            continue
        if c == "`":
            in_tmpl = True
            i += 1
            continue
        # 识别 function 关键字（whole-word）
        if c == "f" and candidate.startswith("function", i):
            before_ok = i == 0 or not is_word(candidate[i - 1])
            after = i + 8
            after_ok = after >= n or candidate[after] in ("(", " ", "\t", "\n", "\r") or not is_word(candidate[after])
            if before_ok and after_ok:
                # 跳过空白
                j = after
                while j < n and candidate[j].isspace():
                    j += 1
                # 可选的函数名（function name (…) 或匿名 function (…)）
                if j < n and is_word(candidate[j]):
                    while j < n and is_word(candidate[j]):
                        j += 1
                    while j < n and candidate[j].isspace():
                        j += 1
                if j < n and candidate[j] == "(":
                    # 平衡 () 找参数列表结尾
                    pdepth = 1
                    j += 1
                    p_in_str = False
                    p_in_squote = False
                    p_in_tmpl = False
                    while j < n and pdepth > 0:
                        cc = candidate[j]
                        if p_in_str:
                            if cc == "\\":
                                j += 2
                                continue
                            if cc == '"':
                                p_in_str = False
                            j += 1
                            continue
                        if p_in_squote:
                            if cc == "\\":
                                j += 2
                                continue
                            if cc == "'":
                                p_in_squote = False
                            j += 1
                            continue
                        if p_in_tmpl:
                            if cc == "\\":
                                j += 2
                                continue
                            if cc == "`":
                                p_in_tmpl = False
                            j += 1
                            continue
                        if cc == '"':
                            p_in_str = True
                            j += 1
                            continue
                        if cc == "'":
                            p_in_squote = True
                            j += 1
                            continue
                        if cc == "`":
                            p_in_tmpl = True
                            j += 1
                            continue
                        if cc == "(":
                            pdepth += 1
                        elif cc == ")":
                            pdepth -= 1
                        j += 1
                    # 跳过空白
                    while j < n and candidate[j].isspace():
                        j += 1
                    if j < n and candidate[j] == "{":
                        fn_start = i
                        body_end = _find_matching_brace(candidate, j)
                        if body_end is None:
                            i += 1
                            continue
                        fn_text = candidate[fn_start:body_end + 1]
                        placeholder = f"{_FN_PLACEHOLDER_PREFIX}{uuid.uuid4().hex}__"
                        fn_map[placeholder] = fn_text
                        out_parts.append(candidate[last:fn_start])
                        out_parts.append(f'"{placeholder}"')
                        last = body_end + 1
                        i = body_end + 1
                        continue
        i += 1
    out_parts.append(candidate[last:])
    return "".join(out_parts), fn_map


def extract_json(raw: str):
    """从 LLM 回复中提取 ECharts option / 可选 code / 文字解释 + 函数映射。

    返回 ``(option, code, explanation, fn_map)`` 4-tuple：
    - ``option``: 解析后的 dict（若 option 中含 JS 函数字面量，函数值会被替换为占位符字符串）
    - ``code``: LLM 额外输出的 JS / Python 代码块（可能为 ``None``）
    - ``explanation``: 去掉代码块后的文字解释
    - ``fn_map``: ``{占位符: 原始函数字面量}``；空 dict 表示无函数。前端拿到后把占位符还原成真函数再 ``setOption``。

    解析失败返回 ``None``。解析尝试顺序：
    1) ``json.loads(candidate)`` —— 纯 JSON 直接过；
    2) ``_strip_js_functions`` + ``json.loads`` —— option 含 ``function (…) {…}`` 时走这条；
    3) ``_try_fix_json`` + ``json.loads`` —— 注释 / 尾随逗号时走这条；
    4) ``_try_fix_json`` + ``_strip_js_functions`` + ``json.loads`` —— 同时含函数和小毛病时走这条。
    """
    option: Optional[dict] = None
    code: Optional[str] = None
    fn_map: dict = {}

    # 1) 优先：抓 JSON 代码块（带语言标签或不带）。
    json_block = _RE_JSON_FENCE.search(raw)
    if json_block:
        candidate = json_block.group(1)
    else:
        # 2) 兜底：栈式扫描第一个完整 {...}。
        candidate = _scan_json_object(raw)

    if candidate is not None:
        # 路径 1: 纯 JSON
        try:
            option = json.loads(candidate)
        except Exception:
            # 路径 2: 含 JS 函数字面量 —— 抠出换成占位符
            try:
                cleaned, fn_map = _strip_js_functions(candidate)
                option = json.loads(cleaned)
            except Exception:
                # 路径 3: 注释 / 尾随逗号
                try:
                    option = json.loads(_try_fix_json(candidate))
                    fn_map = {}
                except Exception:
                    # 路径 4: 两者兼有
                    try:
                        fixed = _try_fix_json(candidate)
                        cleaned, fn_map = _strip_js_functions(fixed)
                        option = json.loads(cleaned)
                    except Exception:
                        option = None
                        fn_map = {}

    if option is None:
        return None

    # 可选 JS / Python 代码块（仅在 LLM 额外输出时存在；只在 raw 里真有 ``` 时再扫）。
    if "```" in raw:
        m = _RE_JS_FENCE.search(raw) or _RE_ALT_FENCE.search(raw)
        if m:
            code = m.group(1).strip()

    # 解释文本 = 去掉所有代码块后的剩余内容（同样按需扫描）。
    if "```" in raw:
        explanation = _RE_ANY_FENCE.sub("", raw).strip()
    else:
        explanation = raw.strip()
    if len(explanation) > _MAX_EXPLANATION:
        explanation = explanation[:_MAX_EXPLANATION] + "…"
    return option, code, explanation, fn_map


def wrap_code(option: dict, llm_code: str, chart_type: str, fn_map: Optional[dict] = None) -> str:
    """
    生成一段可在前端 chart 容器内直接运行的 JavaScript 代码。
    用户前端只需：
      const chart = echarts.init(document.getElementById('chart'));
      eval(返回代码);  // 或解析后 chart.setOption(option)
    这里返回一份包含 option 与 setOption 的完整代码字符串。

    若 ``fn_map`` 非空，会把 ``json.dumps`` 出来的占位符字符串还原为原始 JS 函数字面量
    （让 ``setOption`` 拿到真函数而不是字符串）。
    """
    option_js = json.dumps(option, ensure_ascii=False, indent=2)
    if fn_map:
        for placeholder, fn_text in fn_map.items():
            # 占位符在 json.dumps 里是带引号的 JSON 字符串；替换为原始 JS 源码
            option_js = option_js.replace(f'"{placeholder}"', fn_text)
    if llm_code and "setOption" in llm_code:
        return llm_code
    return (
        f"// 图表类型：{chart_type}\n"
        f"const option = {option_js};\n"
        f"if (typeof chart !== 'undefined') chart.setOption(option);\n"
    )


# ---------------------- Main ----------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    print(f"[ECharts Agent] starting on http://localhost:{port}")
    # threaded=True：长 LLM 请求不会阻塞其他用户/标签页
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
