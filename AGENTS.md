# AGENTS.md

本项目是基于 Flask + 任意 OpenAI 兼容 LLM 的 ECharts 可视化 Agent。前端 `templates/chat.html`、后端 `app.py`、本地 ECharts 知识库 `knowledge.py`、数据解析 `data_parser.py` 与大模型数据整理 `data_understanding.py` 都在仓库根目录平铺；**架构重构分支**新增了 `prompts/`、`chains/`、`tools/`、`agents/`、`memory/`、`output_parsers/` 六个 LangChain 分层。

## 环境

- **必须使用** `F:\workspace\python\312_venv_echarts-agent` 下的 Python（3.12.9，已装 Flask 3.1.3 / flask-cors 6.0.5 / pandas 3.0.3 / openpyxl 3.1.5 / langchain / langchain-core / langchain-community / pydantic）。
  调用方式：
  - PowerShell: `& "F:\workspace\python\312_venv_echarts-agent\Scripts\python.exe" app.py`
  - 或先 `& "F:\workspace\python\312_venv_echarts-agent\Scripts\Activate.ps1"`，再 `python app.py`。
- **不要**用系统 `python` 或别的 venv 跑 —— 会出现找不到依赖或污染依赖的情况。
- 新加 LangChain 相关依赖见 `requirements.txt`（langchain / langchain-core / langchain-openai / langchain-community / pydantic）。**不要引入 langgraph / langsmith（本项目不做流式 + 链式调用）。

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

## 关键架构事实（LangChain 分层 v2）

本仓库采用 "原始 Flask + LangChain 分层"的混合实现：**核心业务流程不依赖 LangChain 模块**（在无网络/依赖缺失时也能跑起来），而**LangChain 模块提供的链/Tool/Memory/Agent 是可选可扩展点，用于扩展未来的 Agent 能力与对话记忆、LCEL 可组合的链式调用。

### LLM 调用：LangChain ChatOpenAIWrapper

`llm_client.py` 暴露两类接口：
- `call_llm(cfg, messages, ...)` / `call_llm(cfg, ...)` —— 向后兼容，**基于 urllib.request 的 OpenAI 兼容接口（不依赖 LangChain）。
- `ChatOpenAIWrapper` 类 —— LangChain 风格 `ChatOpenAI` 实例封装，支持 `response_format` 参数、`provider` 嗅探、`reasoning_effort` / `thinking` 字段处理。
- `get_llm_wrapper(cfg)` —— 获取全局单例 Wrapper（缓存 ChatOpenAI 实例）。

Provider 推理深度（`_detect_provider`）：
- **openai**（含 OpenAI / DeepSeek / 等 OpenAI 兼容服务）：`""` / `"off"` → 不发；`low` / `medium` / `high` → 透传 `reasoning_effort`
- **ollama**（`:11434` / `/ollama`）：`""` / `"off"` → 显式发送 `reasoning_effort: "none"`；其它值透传
- **glm**（`bigmodel.cn` / `zhipu`）：**字段名改为 `thinking` 对象 —— `""` / `"off"` → `thinking: {type: "disabled"}`；其它 → `thinking: {type: "enabled"}`

推理响应的 `reasoning` / `reasoning_content` 字段会拼到 `raw_reply` 头部 `<think>...</think>`；`_parse_structured_chart` 解析前会把它剥掉。

### 知识库

- `knowledge.py` 的常量（`GENERAL` / `TOOLTIP` / `LEGEND` / `AXIS` 等）是**真实**知识源。`tools/knowledge.py` 把它封装为 LangChain Tool：
  - `create_knowledge_tool()` 返回 `Tool` 实例，`search_knowledge` 也被 Tool 化。
- `get_knowledge_for_type(chart_type)` 会按**按图表类型裁剪 KB**（pie/funnel/sankey 不发 axis，gauge/heatmap/candlestick/boxplot 不发 legend），节省 15-25% token。

### 数据解析：双层

1. `data_parser.py` —— 纯代码层（pandas / csv / json / 空格嗅探）。结果带 `raw_text`（原始文本片段）和 `source_ext` 字段。
2. `data_understanding.py` —— 大模型二次理解层。前端勾选「🧠 用大模型智能整理」时调用，**也**在 `data.need_understanding=true` 提交到 `/api/chart` 时调用。
   - LLM 输出严格 JSON：`{columns:[{name,type,role,description}], rows, summary, notes}`。
   - 校验失败 / LLM 调用失败**自动回退**到代码解析结果，`understand_method` 标记为 `fallback`、错误写进 `understand_error`。

### 图表生成 Pipeline（LangChain 链 + 结构化输出

`chains/pipeline.py: run_chart_pipeline(cfg, prompt, data, chart_type_hint, style_hint, *, stream=False)` 是核心：

```
┌─1) 数据准备（瞬时）
├─2) 智能数据整理（复用 / 调一次 LLM / 跳过）
├─3) 数据预处理（本地规则，<50ms）
├─4) 选择图表类型（调一次 LLM，可被 chart_type_hint 覆盖）
└─5) 主生成（流式调用 / 解析）
       ↓
   SSE 事件流：stage / delta / done / error
```

阶段 2 触发条件（按优先级）：
- 数据已有 `understand_method ∈ {llm, fallback} → **直接复用**，不重跑
- 提交时 `data.need_understanding=true` → 调一次
- 都没有 → `skipped`

主生成 LLM 调用 `_CHART_RESPONSE_SCHEMA`（`json_schema` 严格模式，强制 `{option: object, content: string}`）。

### Prompt 管理

`prompts/` 提供：
- `data_understanding.py` —— `SYSTEM_PROMPT` + 模板化用户输入（cols / rows / hint / raw 等）。
- `chart_type.py` —— 图表类型选择模板。
- `chart_generation.py` —— `build_chart_user_prompt(prompt, data, chart_type, style_hint, knowledge, preprocess_info)`。

全部用 LangChain 的 `ChatPromptTemplate` / `SystemMessagePromptTemplate` / `HumanMessagePromptTemplate` 组成。

### 结构化输出 + 5 层兜底（`output_parsers/chart_parser.py`）

解析顺序：
1. `json.loads(raw)` → 拿 `(option, content)` → `parse_method = "primary"`
2. 扫 ` ```json...``` ` 围栏 → 围栏里是 `{option, content}` → `parse_method = "fence_full"`
3. 围栏里只是裸 ECharts option → `parse_method = "fence_option"`
4. 对象是 ECharts option 形态且顶层有 `content` → 抽出 `content` → `parse_method = "in_option"`
5. 围栏 + content-in-option 变种 → `parse_method = "fence_in_option"`
6. 还不行 → 返回 `(None, None, None, error)`；前端根据 `error` 做 502 拒绝。

`done` 事件带 `parse_method` 字段，前端非 `primary` 时在顶部理由行显示「⚠ LLM 偏离 schema」红色徽章。

### 前端与后端的契约

- `/api/parse`：multipart 提交 `file` 或 `text`；可选 `use_llm=1` + `hint=<用户需求>`。返回里 `columns` 既可能是字符串列表（旧格式），也可能是 `[{name,type,role,description}]`（LLM 整理后）；`llm_client.py:build_chart_prompt` 已兼容两种。
- `/api/chart` 与 `/api/chart/stream`：body 解析在 `_parse_chart_request()` 里统一做（缺 prompt 又缺 data 时 400）。两者共用同一个 `run_chart_pipeline`；stream 走 SSE 推 stage/delta/done 事件。
- `done` 事件字段：`{ chart_type, type_reason, option, code, content, explanation, raw_reply, parse_method, understanding?, preprocess? }`。`explanation` 是 `content` 的兼容别名（老前端 / 历史消息用）。
- 静态资源限速：`MAX_CONTENT_LENGTH = 50 * 1024 * 1024`（50MB）。

### LangChain 模块的使用方式

| 模块 | 文件 | 典型用法 | 备注 |
| --- | --- | --- | --- |
| **Prompt** | `prompts/*.py` | `build_data_understanding_prompt()` 返回 `ChatPromptTemplate` | 模板化的用户输入由 `format_data_understanding_input()` 组装 |
| **Chain** | `chains/*.py` | `run_chart_pipeline(...)` → 生成器产出 SSE 事件 | `chains/pipeline.py` 是主入口 |
| **Tools** | `tools/*.py` | `create_knowledge_tool()` → `Tool` 实例 | 可组合进 Agent 做自主决策 |
| **Agent** | `agents/dataviz_agent.py` | `run_dataviz_agent(cfg, user_input, data, chat_history)` | 当前为简化版：直接调 LLM（不需要 Tool 调用链路） |
| **Memory** | `memory/chat_memory.py` | `ChatMemory("default")` 对象 | 会话级记忆（简化版，不依赖 LangChain） |
| **Output Parsers** | `output_parsers/chart_parser.py` | `parse_chart_response(raw)` → `(option, content, parse_method, error)` | 5 层兜底 |

## 工作流注意

- 修改 `data_parser.py` 的 `parse_text`：本分支里有一个我已修复的 `i` 名字未绑定的旧 bug（在「按空格分隔」分支），不要回退这个修复。
- 修改 `data_understanding.py` 的 prompt 时：`SYSTEM_PROMPT` 末尾要求「**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀」。这块约束改了，JSON 抽取可能失效。同时 `llm_client.py` 的 `build_chart_prompt` 末尾也有对应的约束。
- 修改 Prompt 时记得同时看 `llm_client.py: pick_chart_type` 与 `build_chart_prompt` —— 它俩都假设 `data["columns"]` 可能是 schema dict 列表。
- 修改前端样式：在 `static/app.css` 末尾追加即可；不要再拆出独立 CSS 文件。
- **不要**在主路径引入 `langchain.agents.initialize_agent`（仓库没装 langgraph / langsmith；现有 tools / langchain 完整 Agent 用的是 create_openai_functions_agent，但 **agent = 创建了独立版本与 chat openai 函数创建）。

## 调试技巧

- 不启动 Flask 也能跑解析单测：
  ```python
  import sys; sys.path.insert(0, r"F:\project\echarts-agent")
  from data_parser import parse_data_text
  from data_understanding import _extract_json, _validate
  from output_parsers.chart_parser import parse_chart_response  # 新 5 层兜底
  from agents.dataviz_agent import create_dataviz_agent
  ```
- 启服务做端到端：`client = app.test_client()`，直接 `client.post("/api/parse", data={...}, content_type="multipart/form-data")`。
- LLM 调用慢时默认会等 300s；测试用数据用 `< 30` 行的 CSV 即可。
- 临时跑全量回归：
  ```python
  import sys; sys.path.insert(0, r"F:\project\echarts-agent")
  from app import _parse_structured_chart
  from chains.pipeline import run_chart_pipeline
  # 1a 主路径；1b 围栏 + 完整；1c 围栏 + 裸 option；1d content-in-option
  # 2a response_format 透传；2b 降级
  # 3a /api/chart 端到端（parse_method=primary）；3b/3c parse_method 其它值
  ```
- 数据库查看：`python -c "import sqlite3; c=sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"`
- 看 LangChain 链运行：`from chains.understanding import build_data_understanding_chain; chain = build_data_understanding_chain(cfg); chain.invoke({"cols": [...], "rows": [...]})`
