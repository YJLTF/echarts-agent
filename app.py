#!/usr/bin/env python3
import concurrent.futures
import json
import os
import io
import sqlite3
import secrets
from datetime import datetime
from functools import wraps

import pandas as pd
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    render_template,
    session,
    redirect,
    url_for,
    abort,
)
from flask_cors import CORS

from llm_client import call_llm, build_chart_prompt, pick_chart_type
from data_parser import parse_upload, parse_data_text, list_excel_sheets
from data_understanding import understand_data
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
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on", "y")


_DEFAULT_SYSTEM_PROMPT = """你是一名资深的 Apache ECharts 图表工程师，擅长把数据快速转化成生产级 ECharts option。

【硬约束】
1) 只输出一个 JSON 对象，用 ```json ... ``` 包裹；JSON 内不要写注释、不要 markdown 链接。
2) 严格使用用户给的数据，不编造、不替换；xAxis/yAxis 的 data 与列名一致；数值不要用字符串。
3) 数值轴用 type:'value'；类别轴 type:'category' 并提供 data 数组；时间轴 type:'time'。
4) 配色统一用 ECharts 默认调色板（#5470c6 #91cc75 #ee6666 #73a0fa #fac858 #3ba272 等）；用户没指定时不要硬编色名。
5) 标题/副标题/网格/tooltip/legend 按需配置；不输出你无法控制的字段（CDN URL、外部图片、_placeholder 之类）。
6) JSON 之后用 30-80 字中文简要说明该图表达的核心信息（最大/最小/趋势/对比），不要复述数据。

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
    text = request.form.get("text") or (request.get_json(silent=True) or {}).get("text")

    use_llm = parse_bool(
        request.form.get("use_llm")
        or request.args.get("use_llm")
        or (request.get_json(silent=True) or {}).get("use_llm")
    )
    no_header = parse_bool(
        request.form.get("no_header")
        or request.args.get("no_header")
        or (request.get_json(silent=True) or {}).get("no_header")
    )
    user_hint = (
        request.form.get("hint")
        or request.args.get("hint")
        or (request.get_json(silent=True) or {}).get("hint")
        or ""
    )

    # 用户可能多 sheet xlsx 选完后再次提交
    selected_sheets = request.form.getlist("selected_sheets") or None
    if not selected_sheets:
        # JSON body 路径
        try:
            jb = request.get_json(silent=True) or {}
            ss = jb.get("selected_sheets")
            if isinstance(ss, list):
                selected_sheets = [str(x) for x in ss if x]
        except Exception:
            pass

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
                from werkzeug.datastructures import FileStorage
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
    """
    核心：生成图代码。
    请求体 JSON：
    {
      "prompt": "用户需求",
      "data": { （可选，来自解析结果或直接传入）
        "columns": [...],
        "rows": [...],
        "description": "...",
        "need_understanding": true   （可选：本次生成前先让 LLM 整理数据）
      },
      "chart_type_hint": "bar|line|pie|scatter|...", （可选）
      "style_hint": {"theme":"dark","color":["#..."]} （可选）
    }
    """
    body = request.get_json(force=True) or {}
    prompt = (body.get("prompt") or "").strip()
    data = body.get("data")
    chart_type_hint = body.get("chart_type_hint") or ""
    style_hint = body.get("style_hint") or {}

    if not prompt and not data:
        return jsonify({"error": "请至少输入需求或提供数据。"}), 400

    cfg = build_llm_cfg()

    # 0+1) 并行：LLM 整理数据（可选） + 选图表类型
    # 两个调用互相独立（理解结果对选类型有微弱帮助，但用原始数据也够用），
    # 并行可省 30~60s，避免串行 3 调用叠加超过 urllib 超时。
    need_understanding = bool(data and parse_bool(data.get("need_understanding")))
    understanding = None
    chosen_type, type_reason = None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        if need_understanding:
            futures["understand"] = pool.submit(
                understand_data,
                cfg,
                (data or {}).get("raw_text", ""),
                data,
                prompt,
            )
        futures["pick_type"] = pool.submit(
            pick_chart_type, cfg, prompt, data, chart_type_hint
        )

        for name, fut in futures.items():
            try:
                result = fut.result()
            except Exception as e:
                if name == "understand":
                    understanding = {"method": "fallback", "error": f"{e}", "summary": "", "notes": ""}
                else:  # pick_type
                    chosen_type, type_reason = "bar", f"LLM 调用失败，默认使用柱状图：{e}"
                continue
            if name == "understand":
                data = result
                understanding = {
                    "method": result.get("understand_method"),
                    "summary": result.get("summary", ""),
                    "notes": result.get("notes", ""),
                    "error": result.get("understand_error"),
                }
            else:  # pick_type
                chosen_type, type_reason = result

    # 兜底：两个调用都失败时（极少见），避免后续崩
    if not chosen_type:
        chosen_type, type_reason = "bar", "未能推荐图表类型，默认使用柱状图。"
    if understanding is None and need_understanding:
        understanding = {"method": "skipped", "summary": "", "notes": "", "error": None}

    # 2) 检索本地知识库中该图表的配置项说明
    kb = get_knowledge_for_type(chosen_type)

    # 3) 交给 LLM 生成 ECharts option 与可运行代码
    gen_prompt = build_chart_prompt(prompt, data, chosen_type, style_hint, kb)
    sys_prompt = cfg["system_prompt"] or _DEFAULT_SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": gen_prompt},
    ]
    try:
        raw = call_llm(cfg, messages=messages, max_tokens=cfg["max_tokens"], temperature=cfg["temperature"])
    except Exception as e:
        return jsonify({"error": f"模型调用失败：{e}"}), 500

    # 4) 从 LLM 回复中提取 JSON option
    parsed = extract_json(raw)
    retried = False
    if parsed is None:
        # 一次自动重试：把坏掉的回复丢回去让 LLM 修正
        try:
            new_raw, fixed = _auto_retry_parse_json(cfg, sys_prompt, raw)
            if fixed is not None:
                raw = new_raw
                parsed = fixed
                retried = True
            else:
                return jsonify(
                    {
                        "error": "无法从模型回复中解析出 JSON 配置（已尝试一次自动修正，仍未通过）。",
                        "raw_reply": raw,
                    }
                ), 502
        except Exception as e:
            return jsonify(
                {
                    "error": f"自动修正失败：{e}",
                    "raw_reply": raw,
                }
            ), 500

    option, code, explanation = parsed
    chart_js = wrap_code(option, code or "", chosen_type)

    # 5) 保存一条消息到默认会话
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

    resp = {
        "chart_type": chosen_type,
        "type_reason": type_reason,
        "option": option,
        "code": chart_js,
        "explanation": explanation,
        "raw_reply": raw,
        "retried": retried,
    }
    if understanding is not None:
        resp["understanding"] = understanding
    return jsonify(resp)


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
def _try_fix_json(text: str) -> str:
    """对 LLM 输出的「几乎对的 JSON」做最小修复：去行/块注释 + 尾随逗号。

    不做引号替换（容易误伤）；只处理最常见的两种错。
    """
    import re
    # 行注释 // ...
    text = re.sub(r"//[^\n]*", "", text)
    # 块注释 /* ... */
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    # 尾随逗号 ,} 或 ,]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def extract_json(raw: str):
    """
    从 LLM 回复中提取三种内容：
    - 一个 JSON：option 配置项
    - 代码块（可选，若 LLM 额外输出）
    - 文字解释（去掉代码/JSON 后的剩余文本）

    提取流程：先按 ```json ... ``` 块匹配 → 否则用最外层 { ... } 包围
    解析失败时尝试 _try_fix_json 修一次小毛病；都失败返 None（由调用方决定要不要 LLM 重试）。
    """
    option = None
    code = None

    import re

    json_block = re.search(r"```(?:json|echarts|javascript)?\s*(\{[\s\S]+?\})\s*```", raw)
    code_block = re.search(r"```javascript\s*([\s\S]+?)\s*```", raw) or re.search(
        r"```(?:js|python)\s*([\s\S]+?)\s*```", raw
    )

    candidate = None
    if json_block:
        candidate = json_block.group(1)
    else:
        # 尝试定位最外层大括号对
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            candidate = raw[first : last + 1]

    if candidate is not None:
        try:
            option = json.loads(candidate)
        except Exception:
            try:
                option = json.loads(_try_fix_json(candidate))
            except Exception:
                option = None

    if option is None:
        return None

    if code_block:
        code = code_block.group(1).strip()

    # 解释文本 = 去掉代码块后的剩余内容，取前 1500 字
    explanation = re.sub(r"```[\s\S]+?```", "", raw).strip()
    if len(explanation) > 1500:
        explanation = explanation[:1500] + "…"
    return option, code, explanation


def _auto_retry_parse_json(cfg: dict, sys_prompt: str, broken_raw: str):
    """当 extract_json 失败时，调一次 LLM 重新生成。

    Returns: (new_raw, parsed) — parsed 可能仍为 None（重试也失败）。
    """
    fix_msg = (
        "你上一轮的回复无法被解析为合法 JSON（常见原因：漏了闭合括号、"
        "JSON 末尾多了文本或表格、混入了多余代码块、行尾有未闭合的 // 注释）。\n"
        f"你上一轮的原始回复（前 4000 字符）：\n```\n{broken_raw[:4000]}\n```\n\n"
        "请基于同一份需求重新输出，**严格遵守**：\n"
        "1) 只输出一个用 ```json ... ``` 包裹的合法 JSON 对象；\n"
        "2) JSON 内不要写任何注释（// 或 /* */）；\n"
        "3) 不要输出多余的解释文字、表格或第二个代码块；\n"
        "4) 确保所有大括号、中括号、双引号都正确闭合。"
    )
    new_raw = call_llm(
        cfg,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": fix_msg},
        ],
        max_tokens=cfg["max_tokens"],
        temperature=max(0.3, cfg.get("temperature") or 0.7),  # 略偏向确定性，便于稳定
    )
    return new_raw, extract_json(new_raw)


def wrap_code(option: dict, llm_code: str, chart_type: str) -> str:
    """
    生成一段可在前端 chart 容器内直接运行的 JavaScript 代码。
    用户前端只需：
      const chart = echarts.init(document.getElementById('chart'));
      eval(返回代码);  // 或解析后 chart.setOption(option)
    这里返回一份包含 option 与 setOption 的完整代码字符串。
    """
    option_js = json.dumps(option, ensure_ascii=False, indent=2)
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
