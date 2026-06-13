#!/usr/bin/env python3
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
from data_parser import parse_upload, parse_data_text
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
    return render_template("config.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = get_all_config()
    cfg.pop("llm_api_key", None)
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.get_json(force=True) or {}
    for k in (
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "system_prompt",
        "llm_temperature",
        "llm_max_tokens",
    ):
        if k in data and data[k] is not None:
            set_config(k, str(data[k]))
    # 简单连接性测试（可选）
    return jsonify({"ok": True, "saved_keys": list(data.keys())})


@app.route("/api/config/test", methods=["POST"])
def api_config_test():
    cfg = build_llm_cfg()
    if not cfg["api_key"]:
        return jsonify({"ok": False, "message": "未填写 API Key"}), 400
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
    """解析用户上传的文件或直接粘贴的数据文本。"""
    file = request.files.get("file")
    text = request.form.get("text") or (request.get_json(silent=True) or {}).get("text")
    try:
        if file:
            result = parse_upload(file)
        elif text:
            result = parse_data_text(text)
        else:
            return jsonify({"error": "请上传文件或粘贴数据文本。"}), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"数据解析失败：{e}"}), 400


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

    # 1) 决定图表类型
    try:
        chosen_type, type_reason = pick_chart_type(cfg, prompt, data, chart_type_hint)
    except Exception as e:
        chosen_type, type_reason = "bar", f"LLM 调用失败，默认使用柱状图：{e}"

    # 2) 检索本地知识库中该图表的配置项说明
    kb = get_knowledge_for_type(chosen_type)

    # 3) 交给 LLM 生成 ECharts option 与可运行代码
    gen_prompt = build_chart_prompt(prompt, data, chosen_type, style_hint, kb)
    sys_prompt = (
        cfg["system_prompt"]
        or "你是一个资深的数据可视化工程师，擅长使用 Apache ECharts 生成高质量图表。"
        "请严格使用用户指定的数据，并按 ECharts 官方配置项规范输出 JSON。"
    )

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
    if parsed is None:
        return jsonify(
            {
                "error": "无法从模型回复中解析出 JSON 配置。",
                "raw_reply": raw,
            }
        ), 502

    option, code, explanation = parsed
    chart_js = wrap_code(option, code, chosen_type)

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

    return jsonify(
        {
            "chart_type": chosen_type,
            "type_reason": type_reason,
            "option": option,
            "code": chart_js,
            "explanation": explanation,
            "raw_reply": raw,
        }
    )


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
def extract_json(raw: str):
    """
    从 LLM 回复中提取三种内容：
    - 一个 JSON：option 配置项
    - 代码块（可选，若 LLM 额外输出）
    - 文字解释（去掉代码/JSON 后的剩余文本）
    """
    option = None
    code = None

    # 1) 先尝试 ```json ... ```
    import re

    json_block = re.search(r"```(?:json|echarts|javascript)?\s*(\{[\s\S]+?\})\s*```", raw)
    code_block = re.search(r"```javascript\s*([\s\S]+?)\s*```", raw) or re.search(
        r"```(?:js|python)\s*([\s\S]+?)\s*```", raw
    )

    try:
        if json_block:
            option = json.loads(json_block.group(1))
        else:
            # 尝试定位最外层大括号对
            first = raw.find("{")
            last = raw.rfind("}")
            if first >= 0 and last > first:
                candidate = raw[first : last + 1]
                option = json.loads(candidate)
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
    app.run(host="0.0.0.0", port=port, debug=False)
