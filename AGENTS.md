# AGENTS.md

本项目是基于 Flask + 任意 OpenAI 兼容 LLM 的 ECharts 可视化 Agent。前端 `templates/chat.html`、后端 `app.py`、本地 ECharts 知识库 `knowledge.py`、数据解析 `data_parser.py` 与大模型数据整理 `data_understanding.py` 都在仓库根目录平铺，无包结构。

## 环境

- **必须使用** `F:\workspace\python\312_venv_echarts-agent` 下的 Python（3.12.9，已装 Flask 3.1.3 / flask-cors 6.0.5 / pandas 3.0.3 / openpyxl 3.1.5）。
  调用方式：
  - PowerShell: `& "F:\workspace\python\312_venv_echarts-agent\Scripts\python.exe" app.py`
  - 或先 `& "F:\workspace\python\312_venv_echarts-agent\Scripts\Activate.ps1"`，再 `python app.py`。
- **不要**用系统 `python` 或别的 venv 跑 —— 会出现找不到依赖或污染依赖的情况。
- 没有 `pyproject.toml` / `requirements.txt` pin 之外的依赖；新加包请同时更新 `requirements.txt`。

## 启动 & 验证

- 启动：`python app.py`，默认端口 `8080`，可用 `PORT=8000` 覆盖；监听 `0.0.0.0`。
- 浏览器首页 `http://127.0.0.1:8080/` 走对话页；`/config` 走配置页。
- **首次启动必须先在 `/config` 填 Base URL / API Key / 模型名**，否则 `/api/chart` 会返回 428 `need_config: true`。
- 配置存到 `app.db`（SQLite，已在 `.gitignore`）的 `config` 表；**不要**把 `app.db` 提交进仓库。
- `app.db` 同时保存 `chats` / `messages` 表充当对话历史（项目只有一条默认 `default` 对话）。

## 没有的基建（请不要去找）

- 无测试框架：仓库里没有 `tests/`、`pytest.ini`、任何 `*_test.py`。改完代码用 `python -c "import app; client=app.test_client(); ..."` 直接调 Flask test client 验证即可。
- 无 lint / typecheck：没有 `ruff` / `flake8` / `mypy` / `pyright.toml` / `.pre-commit-config.yaml` / CI 工作流。`app.py` / `data_parser.py` 里残留的 pandas/flask import 解析错误是 LSP 看不到依赖所致，**运行时无影响**。
- 无前端构建：`static/app.js`、`static/app.css` 是被 Flask 直接 `send_from_directory` 服务的纯静态文件；改完刷新页面即生效，**不要**引入 npm/webpack。
- 无 DB 迁移：`init_db()` 用的 `CREATE TABLE IF NOT EXISTS`，改表结构时手动 `DROP TABLE` 或清掉 `app.db`。

## 关键架构事实

### 数据解析是「双层」

1. `data_parser.py` —— 纯代码层（pandas / csv / json / 空格嗅探）。所有上传/粘贴的输入都先走这里，结果带 `raw_text`（原始文本片段）和 `source_ext` 字段。
2. `data_understanding.py` —— 大模型二次理解层。前端勾选「🧠 用大模型智能整理」时调用，**也**在 `data.need_understanding=true` 提交到 `/api/chart` 时调用。
   - LLM 输出严格 JSON：`{columns:[{name,type,role,description}], rows, summary, notes}`。
   - 校验失败 / LLM 调用失败**自动回退**到代码解析结果，`understand_method` 标记为 `fallback`、错误写进 `understand_error`。
   - 单位/千分位/百分号在 `_to_number()` 里被剥掉（`万/亿/%/元/¥` 等）。

### 知识库

- `knowledge.py` 的常量（`GENERAL` / `TOOLTIP` / `LEGEND` / `AXIS` 等）是**真实**知识源。代码里 `KB_FILE = ".../knowledge_base.json"`，**这个 JSON 不存在也无所谓** —— `search_knowledge` / `get_knowledge_for_type` 找不到时会落到内置常量，运行时是好的。
- 不要为了"修好" JSON 引用去 `git checkout` 一个 `knowledge_base.json`，它不是必需文件。

### 前端与后端的契约

- `/api/parse`：multipart 提交 `file` 或 `text`；可选 `use_llm=1` + `hint=<用户需求>`。返回里 `columns` 既可能是字符串列表（旧格式），也可能是 `[{name,type,role,description}]`（LLM 整理后）；`llm_client.py:build_chart_prompt` 已经兼容两种。
- `/api/chart`：JSON body；`data.need_understanding=true` 会先调 LLM 整理。返回里 `understanding: {method, summary, notes, error}` 在整理被执行时存在。
- 前端 `static/app.js` 把 `useLlmChk` 复选框状态、清空按钮、生成按钮提示都集中管理；改交互时优先看这里。
- 静态资源限速：`MAX_CONTENT_LENGTH = 50 * 1024 * 1024`（50MB），改大要同步考虑 LLM token 预算。

### LLM 调用

- 全部走 `urllib.request` 调 OpenAI 兼容 `/chat/completions`（无 `requests` 依赖），超时 120s；详见 `llm_client.py: call_llm`。
- 配 Anthropic 兼容时会被 `call_llm` 末尾的兜底分支识别（`content` 字段是 list），但默认按 OpenAI 走。

## 工作流注意

- 修改 `data_parser.py` 的 `parse_text`：本分支里有一个我已修复的 `i` 名字未绑定的旧 bug（在「按空格分隔」分支），不要回退这个修复 —— 退回会导致纯空格分隔的文本数据解析抛 `cannot access local variable 'i'`。
- 修改 `data_understanding.py` 的 prompt 时：`SYSTEM_PROMPT` 末尾要求「**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀」。这块约束改了，JSON 抽取可能失效。
- 修改前端样式：在 `static/app.css` 末尾追加即可；不要再拆出独立 CSS 文件。
- 改 prompt 时记得同时看 `llm_client.py: pick_chart_type` 与 `build_chart_prompt` —— 它俩都假设 `data["columns"]` 可能是 schema dict 列表。

## 调试技巧

- 不启动 Flask 也能跑解析单测：
  ```python
  import sys; sys.path.insert(0, r"F:\project\echarts-agent")
  from data_parser import parse_data_text
  from data_understanding import _extract_json, _validate
  ```
- 启服务做端到端：`client = app.test_client()`，直接 `client.post("/api/parse", data={...}, content_type="multipart/form-data")`。
- LLM 调用慢时默认会等 120s；测试用数据用 `< 30` 行的 CSV 即可。
- 数据库查看：`python -c "import sqlite3; c=sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"`
