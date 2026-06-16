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

### 主生成 LLM 输出：结构化输出 + 三层兜底

主生成阶段（`run_chart_pipeline` 第 5 步）调 LLM 时带 **`_CHART_RESPONSE_SCHEMA`**（`response_format: {type: "json_schema", ...}`），强制模型按 `{option: {...}, content: "..."}` 单层 JSON 输出。`llm_client.call_llm` / `call_llm_stream` 还会做 **服务端降级链** `json_schema → json_object → 不带`（HTTP 400/422 含 `response_format` 关键词触发）。

后端 `_parse_structured_chart(raw)` 按这个顺序兜底解析：

1. **主路径**：`json.loads(raw)` 一次 → 拿 `(option, content)`。`parse_method = "primary"`
2. **围栏**：扫 ` ```json...``` ` 围栏，里面已是 `{option, content}` → `parse_method = "fence_full"`；里面只是裸 ECharts option → 整段当 option，**围栏外文字**当 content → `parse_method = "fence_option"`
3. **content 抽取**：对象是 ECharts option 形态（`series`/`title`/...）且顶层有 `content` 字符串字段 → 抽出 `content`、剩余当 option → `parse_method = "in_option"`（围栏变种为 `"fence_in_option"`）

ECharts option schema 里**没有** `content` 字段，所以"从 option 里抽 `content`"是安全的。

`done` 事件里带 `parse_method: primary / in_option / fence_full / fence_option / fence_in_option` —— 前端非 `primary` 时在顶部理由行显示「⚠ LLM 偏离 schema」红色徽章。

**注意**：本项目**没有自动重试**（之前老版本有 `_retry_parse_json` 调一次 LLM 重写，已删除）；结构化输出靠服务端兜底解析，不再让 LLM 再写一遍。

### 前端与后端的契约

- `/api/parse`：multipart 提交 `file` 或 `text`；可选 `use_llm=1` + `hint=<用户需求>`。返回里 `columns` 既可能是字符串列表（旧格式），也可能是 `[{name,type,role,description}]`（LLM 整理后）；`llm_client.py:build_chart_prompt` 已经兼容两种。
- `/api/chart` 与 `/api/chart/stream`：body 解析在 `_parse_chart_request()` 里统一做（缺 prompt 又缺 data 时 400）。两者共用同一个 `run_chart_pipeline`；stream 走 SSE 推 stage/delta/done 事件。
- `done` 事件字段：`{ chart_type, type_reason, option, code, content, explanation, raw_reply, parse_method, understanding?, preprocess? }`。`explanation` 是 `content` 的兼容别名（老前端 / 历史消息用）。
- 前端 `static/app.js` 把 `useLlmChk` 复选框状态、清空按钮、生成按钮提示都集中管理；改交互时优先看这里。
- 静态资源限速：`MAX_CONTENT_LENGTH = 50 * 1024 * 1024`（50MB），改大要同步考虑 LLM token 预算。

### LLM 调用

- 全部走 `urllib.request` 调 OpenAI 兼容 `/chat/completions`（无 `requests` 依赖），超时 300s；详见 `llm_client.py: call_llm`。
- `call_llm` / `call_llm_stream` 共享 `_post_chat_completion` / `_build_payload` / `_extract_content_text` 三个 helper；`_should_drop_response_format` 判定服务端拒绝 `response_format` 时按 `json_schema → json_object → 不带` 降级。
- 推理深度控制：DB 字段 `llm_thinking` 接受 `""` / `"off"` / `"low"` / `"medium"` / `"high"`（白名单见 `_LLM_THINKING_ALLOWED`）。`build_llm_cfg()` 把它转成 `cfg["reasoning_effort"]`，并叠加 `cfg["provider"]`（由 `_detect_provider(base_url)` 自动嗅探）。`_resolve_thinking_field` 按 provider 把 `llm_thinking` 翻译成 ``(field, value)`` 元组写到 payload：
  - **provider = openai**（含 OpenAI / DeepSeek / DashScope / Qwen 等兼容服务）：`""` / `"off"` → **不发送**；`"low"` / `"medium"` / `"high"` → 透传 `reasoning_effort`
  - **provider = ollama**（Base URL 含 `:11434` 或路径含 `/ollama`）：`""` / `"off"` → 发 `reasoning_effort: "none"` 显式关闭（Ollama 缺省 ≠ 关闭，不发反而会思考）；`"low"` / `"medium"` / `"high"` → 透传
  - **provider = glm**（Base URL 含 `bigmodel.cn` / `zhipu` / `zhipuai`）：**字段名换为 `thinking`** —— `""` / `"off"` → `thinking: {type: "disabled"}`；`"low"` / `"medium"` / `"high"` → `thinking: {type: "enabled"}`（GLM 无粒度差异，统一开启；仅 GLM-4.5+ 生效）
  - 非法值归一化成空（最终按上面规则处理）
- 推理响应：Ollama / GLM / OpenAI 推理模式会在响应里带 `reasoning` 或 `reasoning_content` 字段。`call_llm_raw` 返回 `(content, reasoning)`；主生成路径把 `<think>reasoning</think>` 拼到 `raw_reply` 头部，让用户能在「原始回复」tab 看到思考过程。`_parse_structured_chart` 在解析前先把 `<think>...</think>` 块剥掉，避免污染 JSON 解析。
- 嗅探规则（`_detect_provider`）：匹配 `bigmodel.cn` / `zhipu` / `zhipuai` → `glm`；`:11434` / `/ollama` → `ollama`；其它 → `openai`。服务端忽略未知字段（`reasoning_effort` 对非推理模型是 no-op），不报错。
- 配 Anthropic 兼容时会被 `_extract_content_text` 的 list 分支识别（`content` 字段是 list），但默认按 OpenAI 走。

## 工作流注意

- 修改 `data_parser.py` 的 `parse_text`：本分支里有一个我已修复的 `i` 名字未绑定的旧 bug（在「按空格分隔」分支），不要回退这个修复 —— 退回会导致纯空格分隔的文本数据解析抛 `cannot access local variable 'i'`。
- 修改 `data_understanding.py` 的 prompt 时：`SYSTEM_PROMPT` 末尾要求「**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀」。这块约束改了，JSON 抽取可能失效。
- 修改 `_DEFAULT_SYSTEM_PROMPT` 时：示例必须**完整**用 `{option: {...}, content: "..."}` 包装形态（不要只写裸 ECharts option），不然 LLM 会学到错误模式把 `content` 塞进 option 内部。前端依赖 `parse_method` 字段给用户提示。
- 修改前端样式：在 `static/app.css` 末尾追加即可；不要再拆出独立 CSS 文件。
- 改 prompt 时记得同时看 `llm_client.py: pick_chart_type` 与 `build_chart_prompt` —— 它俩都假设 `data["columns"]` 可能是 schema dict 列表。

## 调试技巧

- 不启动 Flask 也能跑解析单测：
  ```python
  import sys; sys.path.insert(0, r"F:\project\echarts-agent")
  from data_parser import parse_data_text
  from data_understanding import _extract_json, _validate
  from app import _parse_structured_chart  # 主生成的解析，3 种兜底
  ```
- 启服务做端到端：`client = app.test_client()`，直接 `client.post("/api/parse", data={...}, content_type="multipart/form-data")`。
- LLM 调用慢时默认会等 300s；测试用数据用 `< 30` 行的 CSV 即可。
- 临时跑全量回归：
  ```python
  import sys; sys.path.insert(0, r"F:\project\echarts-agent")
  from app import _parse_structured_chart
  # 1a 主路径；1b 围栏 + 完整；1c 围栏 + 裸 option；1d content-in-option
  # 2a  response_format 透传；2b 降级
  # 3a  /api/chart 端到端 (parse_method=primary)；3b/3c parse_method 其它值
  ```
- 数据库查看：`python -c "import sqlite3; c=sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"`
