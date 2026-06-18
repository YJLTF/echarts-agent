#!/usr/bin/env python3
import io
import json
import os
import re
import sqlite3
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Optional

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

from llm_client import call_llm, call_llm_stream, call_llm_raw, build_chart_prompt, get_llm_wrapper, pick_chart_type
import llm_client
from data_parser import parse_upload, parse_data_text, list_excel_sheets
from data_understanding import understand_data
from data_preprocessing import preprocess_data
from knowledge import search_knowledge, get_knowledge_for_type
import prompts

# PyInstaller 冻结后：``__file__`` 指向 _MEIPASS（只读、临时）。
# 资源文件（templates / static）从 ``sys._MEIPASS`` 取；
# 运行时数据（app.db、用户上传文件）放到 %LOCALAPPDATA% 下，避免只读问题。
_IS_FROZEN = getattr(sys, "frozen", False)
_MEIPASS = getattr(sys, "_MEIPASS", None)

if _IS_FROZEN and _MEIPASS:
    BASE_DIR = _MEIPASS
    if os.name == "nt":
        _user_data_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        _user_data_root = os.path.expanduser("~/.local/share")
    _RUNTIME_ROOT = os.path.join(_user_data_root, "EChartsAgent")
    os.makedirs(_RUNTIME_ROOT, exist_ok=True)
    DATA_DIR = os.path.join(_RUNTIME_ROOT, "data")
    DB_PATH = os.path.join(_RUNTIME_ROOT, "app.db")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
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


# 主生成阶段的结构化输出 schema（用于流式路径的 response_format）。
# 双模式：``option`` (JSON dict) 或 ``code`` (JS 代码字符串) 至少一个非空，``content`` 必填。
# 非流式路径走 ``wrapper.structured(ChartGenerationResponse)`` 内部自带 schema，不用这个常量。
_CHART_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "echarts_chart_response",
        "schema": {
            "type": "object",
            "properties": {
                "option": {
                    "type": ["object", "null"],
                    "description": "mode=option 时的 ECharts JSON 配置对象（不含函数）。",
                    "additionalProperties": True,
                },
                "code": {
                    "type": ["string", "null"],
                    "description": "mode=code 时的完整 JS 代码（含 const option = {...}; chart.setOption(option);）。",
                },
                "content": {
                    "type": "string",
                    "description": "30-80 字中文文字解释该图表表达的核心信息。",
                    "minLength": 1,
                },
            },
            "required": ["content"],
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


def _unescape_braces(s: str) -> str:
    """把 ChatPromptTemplate 风格的 ``{{`` / ``}}`` 占位符还原为单花括号。

    LLM 偶尔会误把 JSON 中的 ``{`` 转义成 ``{{``（受 f-string / 模板语法训练数据副作用），
    直接 ``json.loads`` 会失败。这里做一次轻量修复，再走解析。
    """
    return s.replace("{{", "{").replace("}}", "}")


def _try_parse_json(raw: str) -> Optional[Any]:
    """尝试 ``json.loads``；失败时再做一次 ``{{``/``}}`` 反转义重试。"""
    try:
        return json.loads(raw)
    except Exception:
        pass
    unescaped = _unescape_braces(raw)
    if unescaped != raw:
        try:
            return json.loads(unescaped)
        except Exception:
            pass
    return None


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
        - method 取值：``"primary"`` / ``"primary_bare"`` / ``"in_option"`` /
          ``"fence_full"`` / ``"fence_option"`` / ``"fence_in_option"``；
          非 ``primary`` / ``primary_bare`` 时前端顶部显示「⚠ LLM 偏离 schema」徽章。
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

    # 1) 主路径：直接 json.loads（自动兼容 {{ }} 转义）
    obj = _try_parse_json(raw)
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
        inner_obj = _try_parse_json(inside)
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
    # 顺便给一个明显的「输出被截断」提示，便于快速判断是 LLM 没输出完 vs JSON 格式错误
    trunc_hint = ""
    stripped = raw.strip()
    if stripped and not stripped.endswith(("{", "}", "`", '"', "]")):
        trunc_hint = "（输出似乎被截断 —— max_tokens 可能不够）"
    return None, None, None, f"模型输出不符合结构化 schema{trunc_hint}：JSON.loads 失败或字段缺失（reply: {preview!r}）"


def _parse_chart_response(raw: str) -> tuple[Optional[dict], Optional[str], Optional[str], str, str]:
    """Pydantic 优先的图表响应解析（双模式：返回 option / code / content）。

    Returns:
        (option, code, content, parse_method, error)
        - 成功：error 为 ``""``，``parse_method`` 形如 ``primary`` / ``in_option`` / ``fence_*``
        - 失败：前 3 项为 ``None``，``parse_method`` 为 ``""``，``error`` 描述问题

    路径：

    1. Pydantic 强类型校验 ``ChartGenerationResponse`` —— 支持 ``option`` 或 ``code`` 双模式
    2. 5 层手写兜底（仅返回 option，不识别 code）

    兼容：原始 raw 含 ``{{`` / ``}}`` 转义（LLM 受 f-string 训练数据副作用）时，
    会先按 unescape 后的版本再校验一次 Pydantic。
    """
    if not raw or not raw.strip():
        return None, None, None, "", "LLM 返回为空"

    from output_parsers.schema import ChartGenerationResponse

    # 路径 1：Pydantic 强类型校验（支持 option / code 双模式）。
    # 先试 raw；失败时再试 unescape 后的版本（兼容 LLM 把 { 转义成 {{ 的副作用）。
    candidates = [raw]
    unescaped = _unescape_braces(raw)
    if unescaped != raw:
        candidates.append(unescaped)
    for candidate in candidates:
        try:
            result = ChartGenerationResponse.model_validate_json(candidate, strict=False)
            if result.content and (result.option or result.code):
                return result.option, result.code, result.content, "primary", ""
        except Exception:
            pass

    # 路径 2：老 5 层手写兜底（仅识别 option 模式；{{ }} 自动兼容）
    opt, content, method, err = _parse_structured_chart(raw)
    return opt, None, content, method or "", err


# 推理深度合法取值。空串 / off / low / medium / high —— "off" 与空串都视为"不发送"。
_LLM_THINKING_ALLOWED = {"", "off", "low", "medium", "high"}

# Provider 显式选择。空串 = 自动嗅探；其它值覆盖嗅探结果。
_LLM_PROVIDER_ALLOWED = {"", "openai", "ollama", "glm"}


def _normalize_thinking(v: object) -> str:
    """把用户/DB 里的 llm_thinking 值归一化成合法取值。"""
    s = str(v or "").strip().lower()
    return s if s in _LLM_THINKING_ALLOWED else ""


def _normalize_provider(v: object) -> str:
    """把用户/DB 里的 llm_provider 值归一化成合法取值。空串 = 自动嗅探。"""
    s = str(v or "").strip().lower()
    return s if s in _LLM_PROVIDER_ALLOWED else ""


def build_llm_cfg() -> dict:
    cfg = get_all_config()
    thinking = _normalize_thinking(cfg.get("llm_thinking"))
    base_url = cfg.get("llm_base_url", "").strip().rstrip("/")
    explicit_provider = _normalize_provider(cfg.get("llm_provider"))
    # 用户在配置页显式选过 provider 时优先使用；否则走 URL 嗅探。
    # 嗅探结果缓存：相同 base_url 走相同分支，避免每次 pipeline 重算。
    provider = explicit_provider or llm_client._detect_provider(base_url)
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
        "provider": provider,
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
    # 让前端在「自动」模式下能展示按 Base URL 嗅探出的实际 provider，
    # 方便用户判断要不要手动改。
    base_url = (cfg.get("llm_base_url") or "").strip()
    cfg["llm_provider_detected"] = llm_client._detect_provider(base_url) if base_url else ""
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
        "llm_provider",
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
        elif k == "llm_provider":
            # provider 显式选择：空串 = 自动嗅探；非法值忽略
            v = _normalize_provider(v)
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
    _apply_body_overrides(cfg, body)

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


def _apply_body_overrides(cfg: dict, body: dict) -> None:
    """把请求体里非空的字段覆盖到 ``cfg`` 上（``api_config_test`` 用）。"""
    def _set_str(key: str, target: str, transform=str.strip):
        v = body.get(key)
        if v:
            cfg[target] = transform(str(v))

    _set_str("llm_base_url", "base_url", lambda s: s.strip().rstrip("/"))
    _set_str("llm_api_key", "api_key")
    _set_str("llm_model", "model")

    if "llm_provider" in body and body["llm_provider"] is not None:
        explicit = _normalize_provider(body["llm_provider"])
        cfg["provider"] = explicit or llm_client._detect_provider(cfg["base_url"])

    for src, dst, cast in (
        ("llm_temperature", "temperature", float),
        ("llm_max_tokens", "max_tokens", int),
    ):
        if src in body and body[src] is not None:
            try:
                cfg[dst] = cast(body[src])
            except (TypeError, ValueError):
                pass

    if "llm_thinking" in body and body["llm_thinking"] is not None:
        norm = _normalize_thinking(body["llm_thinking"])
        cfg["reasoning_effort"] = "" if norm in ("", "off") else norm


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
    text = _get_param("text")

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
    - 所有异常都被包住 → 以 ``{"type":"error"}`` 事件正常结束流，而不是
      让底层抛出异常把 TCP 连接打断，避免前端报 "读取流失败"。
    """
    prompt, data, chart_type_hint, style_hint = _parse_chart_request()
    cfg = build_llm_cfg()

    def generate():
        # 先推一条注释，让代理/浏览器建立流状态
        yield ": ok\n\n"
        try:
            for evt in run_chart_pipeline(
                cfg, prompt, data, chart_type_hint, style_hint, stream=True
            ):
                yield _sse_format_event(evt)
                if evt.get("type") in ("done", "error"):
                    return
        except Exception as e:
            # pipeline 生成器自身抛异常（不应发生，但兜底）
            traceback.print_exc()
            yield _sse_format_event(
                {"type": "error", "message": f"生成失败：{e}"}
            )
            return

        # pipeline 正常结束但没产出 done/error → 兜底
        yield _sse_format_event(
            {"type": "error", "message": "生成失败：服务端异常中断"}
        )

    resp = Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
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
    # 把预处理结果挂在 data 上，让 build_chart_user_prompt 内部自动加上精度提示。
    if has_applied and isinstance(data, dict):
        data = {**data, "preprocess": preprocess_info}
    kb = get_knowledge_for_type(chosen_type)
    gen_prompt = build_chart_prompt(prompt, data, chosen_type, style_hint, kb)

    sys_prompt = cfg["system_prompt"] or prompts.DEFAULT_CHART_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": gen_prompt},
    ]

    # ---- 6) 主生成：调用 LLM（流式或一次性） ----
    yield {"type": "stage", "stage": "generate", "label": "生成图表配置", "status": "start"}
    raw = ""
    stream_failed = False
    stream_error_msg = ""
    try:
        if stream:
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
                    # LLM 流式调用中途失败 —— 记录错误，但不立即 return
                    # 而是尝试用已收到的部分结果继续解析（降级）
                    stream_failed = True
                    stream_error_msg = str(inner_e)
                    print(f"[pipeline] LLM 流式中途失败: {inner_e} (已收到 {len(raw)} 字符)", flush=True)
                    break
                raw += chunk
                if chunk:
                    yield {"type": "delta", "content": chunk}

            # 流式中途失败的两种情况：
            # 1) 完全没收到数据 → 报错
            # 2) 有部分输出 → 继续走解析路径（前端展示「降级解析」徽章）
            if stream_failed:
                if not raw.strip():
                    yield {"type": "stage", "stage": "generate", "status": "error", "message": stream_error_msg}
                    yield {"type": "error", "message": f"模型流式调用失败：{stream_error_msg}", "raw_reply": raw, "status": 500}
                    return
                yield {
                    "type": "stage", "stage": "generate", "status": "done",
                    "length": len(raw), "degraded": True, "degraded_reason": stream_error_msg,
                }
                yield {"type": "delta", "content": "\n\n[注：模型输出中断，已用部分结果降级解析]"}
        else:
            # 非流式：优先用 ``with_structured_output(ChartGenerationResponse)``
            # —— LangChain 内部自动注入 JSON schema 说明 + 失败重试 + Pydantic 校验
            from output_parsers.schema import ChartGenerationResponse
            try:
                structured = get_llm_wrapper(cfg).structured(
                    ChartGenerationResponse,
                    max_tokens=cfg["max_tokens"],
                    temperature=cfg["temperature"],
                    reasoning_effort=cfg.get("reasoning_effort") or None,
                )
                result = structured.invoke(messages)
                if isinstance(result, ChartGenerationResponse):
                    raw = result.model_dump_json()
                else:
                    # 异常分支 —— 退回去走原始 call_llm_raw + 5 层解析
                    raise RuntimeError("structured() returned non-pydantic")
            except Exception as structured_err:
                print(f"[pipeline] structured() failed: {structured_err}; falling back to call_llm_raw", flush=True)
                raw, _reasoning = call_llm_raw(
                    cfg,
                    messages=messages,
                    max_tokens=cfg["max_tokens"],
                    temperature=cfg["temperature"],
                    response_format=_CHART_RESPONSE_SCHEMA,
                    reasoning_effort=cfg.get("reasoning_effort") or None,
                )
                # 注意：不把 reasoning 拼进 raw —— 解析器会用正则剥掉 <think>，
                # 拼上去只会徒增字符。如果想保留 reasoning，应该单独存。
    except Exception as e:
        print(f"[pipeline] 生成阶段异常: {e}", flush=True)
        yield {
            "type": "stage",
            "stage": "generate",
            "status": "error",
            "message": str(e),
        }
        yield {"type": "error", "message": f"模型调用失败：{e}", "raw_reply": raw, "status": 500}
        return
    if not stream_failed:
        yield {"type": "stage", "stage": "generate", "status": "done", "length": len(raw)}

    # ---- 7) 解析（结构化输出 → 一次 Pydantic 校验即够） ----
    yield {"type": "stage", "stage": "parse", "label": "解析与校验", "status": "start"}
    option, code, content, parse_method, parse_error = _parse_chart_response(raw)
    if parse_error or not content:
        # stage error message 用简短摘要，详细错误由 error 事件 + raw_reply 承载
        short_msg = "模型输出不符合 schema（请看顶部错误提示）" if parse_error else "缺少 content 字段"
        yield {
            "type": "stage",
            "stage": "parse",
            "status": "error",
            "message": short_msg,
        }
        yield {
            "type": "error",
            "message": parse_error or "模型输出缺少 content 字段",
            "raw_reply": raw,
            "status": 502,
        }
        return
    # code 模式：LLM 直接给了 JS 代码（含 chart.setOption(option) 调用）
    #   → 让前端 sandbox 执行，提取 option 用于渲染
    # option 模式：LLM 给了 JSON option
    #   → 用 wrap_code 把它包装成 JS（供"代码"标签页展示）
    chart_js = code or wrap_code(option or {}, chosen_type)

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
        # 区分 code 模式（LLM 直接输出 JS 代码）与 option 模式（仅 JSON）
        # 前端用 is_code_mode 决定是直接 setOption 还是先跑 sandbox 提取
        "is_code_mode": bool(code),
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



def wrap_code(option: dict, chart_type: str) -> str:
    """生成可在前端 chart 容器内直接运行的 JavaScript 代码。

    用户前端只需 ``chart.setOption(option)`` 即可生效（前端从 ``data.code`` 直接复制）。
    """
    option_js = json.dumps(option, ensure_ascii=False, indent=2)
    return (
        f"// 图表类型：{chart_type}\n"
        f"const option = {option_js};\n"
        f"if (typeof chart !== 'undefined') chart.setOption(option);\n"
    )


# ---------------------- Main ----------------------
def _open_browser_when_ready(url: str, delay: float = 1.5) -> None:
    """延迟打开浏览器；用线程避免阻塞 Flask 启动。"""
    import time

    def _runner():
        time.sleep(delay)
        try:
            webbrowser.open_new(url)
        except Exception:
            pass

    threading.Thread(target=_runner, daemon=True).start()


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "127.0.0.1")
    open_browser = parse_bool(os.environ.get("OPEN_BROWSER", "1"))
    print(f"[ECharts Agent] starting on http://{host}:{port}")
    print(f"[ECharts Agent] using Flask dev server (threaded, streaming-friendly)")
    if _IS_FROZEN:
        print(f"[ECharts Agent] data dir: {_RUNTIME_ROOT}")
    if open_browser and host in ("127.0.0.1", "localhost", "0.0.0.0"):
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        _open_browser_when_ready(f"http://{display_host}:{port}/")
    # Flask 开发服务器 + threaded=True 原生支持流式响应逐 chunk flush；
    # waitress 会缓冲响应体，不适合 SSE 流式。
    try:
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[ECharts Agent] shutting down")
        sys.exit(0)
