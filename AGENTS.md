# AGENTS.md · 开发者指南

本文档面向希望在 ECharts Agent 项目上进行开发、调试或定制的工程师。

---

## 环境

- **Python**：建议 3.9+，依赖见 `requirements.txt`
- **启动**：`python app.py`，默认端口 `8080`（`PORT=8000 python app.py` 覆盖）
- **配置**：首次使用必须先在 `/config` 填写 Base URL / API Key / 模型名，否则 `/api/chart` 返回 428
- **配置存储**：`app.db`（SQLite，`CREATE TABLE IF NOT EXISTS`，改表结构需手动 DROP TABLE）
- **不要提交** `app.db`、`.env`、`__pycache__/` 进仓库

---

## 架构总览

项目采用 **Flask + LangChain 分层** 的混合架构：

```
┌─────────────────────────────────────────────────┐
│                  app.py (Flask)                  │
│          对外暴露 /api/* HTTP 接口               │
│         run_chart_pipeline() 事件流生成器         │
│   直接调用 llm_client / data_preprocessing 等     │
└──────────────────────┬──────────────────────────┘
                        │ ← 核心业务流程不依赖 LangChain 模块
┌──────────────────────▼──────────────────────────┐
│              核心业务模块（根目录）                │
│  llm_client.py   data_parser.py                  │
│  data_understanding.py   data_preprocessing.py   │
│  knowledge.py     output_parsers/chart_parser.py  │
└──────────────────────┬──────────────────────────┘
                        │ ← 可选扩展层，按需引入
┌──────────────────────▼──────────────────────────┐
│              LangChain 模块（新增）               │
│   prompts/   chains/   tools/                    │
│   agents/   memory/   output_parsers/            │
└─────────────────────────────────────────────────┘
```

**关键原则**：核心业务流程（`app.py` 的 pipeline）不依赖 `prompts/` / `chains/` / `tools/` 等新增模块——它们是可选扩展点，用于未来扩展 Agent 能力、对话记忆或 LCEL 可组合链式调用。现有 API（`/api/parse` / `/api/chart` / `/api/chart/stream`）无需调整。

---

## 核心业务模块（根目录）

### llm_client.py

**职责**：LLM 调用封装，同时提供 urllib 兼容接口和 LangChain `ChatOpenAI` 封装。

**关键类和函数**：

| 名称 | 签名 | 用途 |
|---|---|---|
| `ChatOpenAIWrapper` | `__init__(base_url, api_key, model, temperature, max_tokens, reasoning_effort)` | LangChain `ChatOpenAI` 实例封装，支持 `response_format` |
| `ChatOpenAIWrapper.call_llm` | `(messages, max_tokens?, temperature?, response_format?, reasoning_effort?) → str` | 非流式调用，只返回 content |
| `ChatOpenAIWrapper.call_llm_raw` | `... → tuple[str, str]` | 返回 `(content, reasoning)` |
| `ChatOpenAIWrapper.call_llm_stream` | `(messages, ...) → Iterator[str]` | 流式调用，逐 chunk 产出 content |
| `ChatOpenAIWrapper.llm` | `property → ChatOpenAI` | 获取底层 `ChatOpenAI` 实例（延迟初始化） |
| `get_llm_wrapper(cfg)` | `(cfg: dict) → ChatOpenAIWrapper` | 全局单例缓存，根据 base_url/api_key/model 缓存实例 |
| `call_llm(cfg, messages, ...)` | `→ str` | 兼容层：内部调用 `get_llm_wrapper(cfg).call_llm(...)` |
| `call_llm_raw(cfg, messages, ...)` | `→ tuple[str, str]` | 兼容层 |
| `call_llm_stream(cfg, messages, ...)` | `→ Iterator[str]` | 兼容层 |
| `pick_chart_type(cfg, prompt, data, hint)` | `→ tuple[chart_type: str, reason: str]` | 让 LLM 推荐图表类型；hint 非空时直接返回 hint |
| `build_chart_prompt(prompt, data, chart_type, style_hint, knowledge)` | `→ str` | 把需求/数据/类型/样式/KB 拼成最终 user prompt |
| `compute_column_stats(rows, column_names)` | `→ str` | 数值列：范围/均值/distinct/分位数；文本列：distinct/top-3；< 20 行返回空 |
| `_detect_provider(base_url)` | `→ "openai" \| "ollama" \| "glm"` | 从 URL 嗅探 provider |
| `_resolve_thinking_field(cfg, user_value)` | `→ Optional[tuple[field, value]]` | 按 provider 把 `llm_thinking` 值转成对应字段（`reasoning_effort` / `thinking`） |

**`_detect_provider` 嗅探规则**：
- 含 `:11434` 或 `/ollama` → `"ollama"`
- 含 `bigmodel.cn` / `zhipuai` / `zhipu` → `"glm"`
- 其它（含 OpenAI / DeepSeek / DashScope / Qwen）→ `"openai"`

**`_resolve_thinking_field` 行为**：

| provider | "" / "off" | "low" / "medium" / "high" |
|---|---|---|
| openai | 不发送 | `reasoning_effort: value` |
| ollama | `reasoning_effort: "none"` | `reasoning_effort: value` |
| glm | `thinking: {type: "disabled"}` | `thinking: {type: "enabled"}` |

### data_parser.py

**职责**：Excel / CSV / JSON / 纯文本的统一解析，输出 `{columns, rows, count, description, raw_text, source_ext}`。

**关键函数**：
- `parse_upload(file, no_header?, selected_sheets?)` → 解析上传文件
- `parse_data_text(text, no_header?)` → 解析粘贴文本（自动嗅探分隔符：tab / 逗号 / 分号 / 多空格）
- `list_excel_sheets(raw_bytes)` → 列出 xlsx/xls 内所有 sheet 名

**输出格式**（两种 columns 形态共存，调用方需兼容）：
1. **旧格式**：字符串列表 `["列名1", "列名2", ...]`
2. **LLM 整理后**：`[{name, type, role, description}, ...]`

`build_chart_prompt` / `pick_chart_type` 已兼容两种形态。

### data_understanding.py

**职责**：LLM 二次理解层，把不规范数据整理成 schema + rows。

**关键函数**：
- `understand_data(cfg, raw_text, parsed, hint?)` → 调用 LLM，返回整理后数据（含 `understand_method: "llm"`）
- 失败时自动回退到 `parsed`，`understand_method` 标记为 `"fallback"`，错误写入 `understand_error`

### data_preprocessing.py

**职责**：从 prompt 识别并应用本地规则（round / drop_null / dedup / group_sum / group_mean / sort / top_n / strip_thousands / iqr_outlier），< 50ms 完成。

**关键函数**：
- `preprocess_data(prompt, data)` → 返回 `(new_data, info)`；`info` 含 `rules`（识别出的规则）/ `applied`（已生效）/ `skipped`（识别失败的）/ `summary`

**支持规则**：

| 规则 | 示例 prompt |
|---|---|
| `round` | `保留 2 位小数` / `round to 2 decimals` |
| `round_col` | `销售额 保留 1 位小数` |
| `drop_null` | `去除空值` / `drop nulls` |
| `dedup` | `去重` / `dedup` |
| `iqr_outlier` | `去除异常值` |
| `group_sum` | `按月份分组求和` |
| `group_mean` | `按月份分组求平均` |
| `sort` | `按销售额降序` |
| `top_n` | `前 10 大` / `top 5` |
| `strip_thousands` | `去掉千分位` |

中文数字归一化（`"两"` → `"2"` / `"十"` → `"10"`）已内置。

### knowledge.py

**职责**：ECharts 配置项知识库，常量 `GENERAL` / `TOOLTIP` / `LEGEND` / `AXIS` 等是**真实知识源**。

**关键函数**：
- `get_knowledge_for_type(chart_type)` → 按图表类型**裁剪 KB** 返回相关章节（pie/funnel/sankey 不发 axis，gauge/heatmap/candlestick/boxplot 不发 legend），节省 15-25% token
- `search_knowledge(q)` → 按关键词全文搜索

**`CHART_USES_AXIS`**：各图表是否需要 xAxis/yAxis 的事实表。

### output_parsers/chart_parser.py

**职责**：ECharts option 5 层解析 + 数据理解 JSON 解析。

**关键函数**：
- `parse_chart_response(raw)` → `tuple[option, content, parse_method, error]`：
  - `primary` / `fence_full` / `fence_option` / `in_option` / `fence_in_option`
- `parse_data_understanding_response(raw)` → `tuple[parsed_data, error_message]`

---

## LangChain 模块（新增）

### prompts/

基于 `langchain_core.prompts` 的 PromptTemplate 集中管理。

| 文件 | 关键导出 | 说明 |
|---|---|---|
| `data_understanding.py` | `SYSTEM_PROMPT`（常量）、`USER_PROMPT_TEMPLATE`（常量）、`build_data_understanding_prompt()`、`format_data_understanding_input()` | 数据理解 prompt；`format_data_understanding_input` 返回格式化后的 dict，keys 对应模板变量 |
| `chart_type.py` | `SYSTEM_PROMPT`（常量）、`build_chart_type_prompt()`、`format_chart_type_input()` | 图表类型选择 prompt |
| `chart_generation.py` | `SYSTEM_PROMPT`（常量）、`build_chart_generation_prompt()`、`build_chart_user_prompt()` | 图表生成 prompt；`build_chart_user_prompt` 是核心，返回拼接后的字符串 prompt |

所有 import 均使用 `langchain_core.prompts`，不使用已废弃的 `langchain.prompts`。

### chains/

**注意**：`app.py` 内部有自己的 `run_chart_pipeline` 实现，不依赖 `chains/pipeline.py`。`chains/` 是独立的 LangChain 风格实现。

| 文件 | 关键导出 | 说明 |
|---|---|---|
| `understanding.py` | `build_data_understanding_chain(cfg)`、`invoke_data_understanding(cfg, parsed, user_hint?, raw_preview?)` | 数据理解 LCEL 链：`format_input \| prompt \| llm \| StrOutputParser \| parse_output` |
| `chart_generation.py` | `CHART_RESPONSE_SCHEMA`（常量）、`build_chart_generation_chain(cfg)`、`generate_chart(cfg, prompt, data, chart_type, ...)`、`generate_chart_stream(...)` | 图表生成链；`CHART_RESPONSE_SCHEMA` 是传给 LLM 的 `response_format` 参数 |
| `pipeline.py` | `run_chart_pipeline(...)` | 新版 6 阶段流水线生成器（event-based），与 `app.py` 内部 pipeline 并行；`stream=True` 时产出 `delta` 事件 |

**`chains/pipeline.py` 与 `app.py:run_chart_pipeline` 的关系**：两者都是 6 阶段流水线，签名相同，但实现独立。`chains/pipeline.py` 使用 `generate_chart_stream` 从 `chains/chart_generation.py`，而 `app.py` 使用 `llm_client.call_llm_raw`。

### tools/

基于 `langchain.tools` 的 Tool 封装。

| 文件 | 关键导出 | 说明 |
|---|---|---|
| `knowledge.py` | `create_knowledge_tool()`、`create_search_knowledge_tool()`、`get_all_tools()` | `Tool` 实例，底层调用 `knowledge.py` 的 `get_knowledge_for_type` / `search_knowledge` |
| `preprocessor.py` | `create_preprocess_tool()`、`get_all_preprocess_tools()` | 数据预处理 Tool；底层调用 `data_preprocessing.preprocess_data` |
| `chart_selector.py` | `create_chart_type_selector_tool()`、`get_all_chart_tools()` | 图表类型选择 Tool；底层调用 `llm_client.pick_chart_type` |

### agents/dataviz_agent.py

**简化版 Agent**：直接调 LLM，不依赖 Tool 调用链路（因为完整 Agent 需要 `langchain.agents` / `langgraph`，版本兼容性问题较多）。

**关键导出**：
- `DATAVIZ_SYSTEM_PROMPT`（常量）：Agent 系统提示
- `create_dataviz_agent(cfg)` → 返回 `ChatOpenAIWrapper` 实例
- `run_dataviz_agent(cfg, user_input, data?, chat_history?)` → `{"success", "output"}` 或 `{"success", "error"}`

### memory/chat_memory.py

**进程内对话记忆**（不依赖 LangChain Memory）。

**关键导出**：
- `get_memory(session_id)` → `List[Dict[str, str]]`：获取会话记忆列表
- `clear_memory(session_id)`：清除会话记忆
- `add_user_message(session_id, message)` / `add_ai_message(session_id, message)`：追加消息
- `ChatMemory(session_id)`：面向对象接口

存储结构：`List[{"role": "user"|"assistant", "content": str}]`。

---

## app.py 关键实现

### 配置读取与构建

- `get_config(key, default?)` / `set_config(key, value)`：读写 SQLite
- `get_all_config()` → `dict`：返回所有 key-value
- `build_llm_cfg()` → `dict`：拼装 LLM 调用所需的完整 cfg（含 `base_url` / `api_key` / `model` / `system_prompt` / `temperature` / `max_tokens` / `reasoning_effort` / `provider`）

### API 路由

| 路由 | 函数 | 说明 |
|---|---|---|
| `GET /` | `index()` | 渲染 `chat.html` |
| `GET /config` | `config_page()` | 同上，但前端会自动打开配置弹窗 |
| `GET /api/config` | `api_config_get()` | 返回配置（Key 只返掩码 + `llm_api_key_present`） |
| `POST /api/config` | `api_config_set()` | 保存配置；Key 为空/null 时不覆盖 |
| `POST /api/config/test` | `api_config_test()` | 测试 LLM；body 可覆盖 DB 值 |
| `POST /api/parse` | `api_parse()` | 解析文件/文本；`use_llm=1` 时调用 `understand_data` |
| `GET /api/knowledge` | `api_knowledge()` | 查询知识库 |
| `POST /api/chart` | `api_chart()` | 非流式生成；遍历 `run_chart_pipeline` 取 `done` 事件 |
| `POST /api/chart/stream` | `api_chart_stream()` | SSE 流式生成；`generate()` 迭代 `run_chart_pipeline`，每事件 `_sse_format_event` |

### Pipeline 事件协议

`run_chart_pipeline(cfg, prompt, data, chart_type_hint, style_hint, *, stream=False)` yields `dict`：

| `type` | 阶段 | 关键字段 |
|---|---|---|
| `stage` | 所有阶段 | `stage`, `status`(start/done/skipped/error), `label` |
| `delta` | generate 阶段（仅 stream=True） | `content`（单次 chunk） |
| `done` | 最终 | `chart_type`, `type_reason`, `option`, `content`, `raw_reply`, `parse_method`, `understanding?`, `preprocess?` |
| `error` | 出错 | `message`, `status`, `raw_reply?` |

---

## 工作流注意事项

### 修改 Prompt

1. `data_understanding.py` 的 `SYSTEM_PROMPT` 末尾要求「**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀」。这块约束改了，JSON 抽取可能失效。
2. `llm_client.py:build_chart_prompt` 末尾有对应的约束，修改时两者要同步。
3. `llm_client.py:pick_chart_type` 的 system prompt 也要求「只回复一个英文单词 + 一行中文理由」，格式变了图表推荐会失效。

### 修改数据解析

`data_parser.py:parse_text` 有一个已修复的 `i` 名字未绑定 bug（在「按空格分隔」分支），不要回退此修复——会导致纯空格分隔数据解析抛 `cannot access local variable 'i'`。

### 引入新依赖

- **禁止**引入 `langgraph` / `langsmith`（项目未装，会导致 ImportError）
- **禁止**引入 `langchain.agents.initialize_agent`（`create_openai_functions_agent` 已存在，但完整 Agent 需要额外配置）
- 所有 LangChain import 应使用 `langchain_core.*` 而非已废弃的 `langchain.*`

### 前端修改

在 `static/app.css` 末尾追加即可；不要再拆出独立 CSS 文件。前端是纯静态文件，Flask 直接 `send_from_directory` 服务，修改后刷新页面即生效，无需构建步骤。

---

## 调试技巧

### 不启动 Flask 的单元测试

```python
import sys
sys.path.insert(0, "/workspace")
from data_parser import parse_data_text
from output_parsers.chart_parser import parse_chart_response, parse_data_understanding_response
from chains.understanding import build_data_understanding_chain, invoke_data_understanding
from agents.dataviz_agent import create_dataviz_agent, run_dataviz_agent
```

### Flask test client 端到端测试

```python
client = app.test_client()
# 解析
resp = client.post("/api/parse", data={"text": "a,b\n1,2\n3,4"}, content_type="multipart/form-data")
print(resp.json)
# 生成
resp = client.post("/api/chart", json={"prompt": "画柱状图", "data": {"columns": ["a","b"], "rows": [{"a":1,"b":2}]}})
print(resp.json)
```

### 流式 SSE 调试

```python
client = app.test_client()
resp = client.post("/api/chart/stream", json={"prompt": "画柱状图"}, stream=True)
for line in resp.response:
    print(line.decode())
```

### LangChain Chain 调试

```python
from chains.understanding import build_data_understanding_chain
cfg = {"base_url": "...", "api_key": "...", "model": "..."}
chain = build_data_understanding_chain(cfg)
result = chain.invoke({"cols": [...], "rows": [...], "hint": "...", "raw": "..."})
print(result)
```

### 数据库查看

```bash
python -c "import sqlite3; c=sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"
```

### LLM 调用慢

默认 HTTP 超时 300s；测试用数据用 `< 30` 行的 CSV 即可。

---

## 测试矩阵（parse_method / response_format 降级）

```
1a) json_schema 严格模式 + json.loads 直接拿到 {option, content}     → parse_method=primary
1b) json_schema + json.loads 但只有 option（LLM 把 content 塞进 option）→ parse_method=in_option
2a) json_object 降级 + json.loads 正常                             → parse_method=primary
2b) json_object 降级 + json.loads 正常但 content in option          → parse_method=in_option
3a) 无 response_format + json.loads 正常                           → parse_method=primary
3b) 无 response_format + 裸 ECharts option                         → parse_method=in_option
4a) LLM 套 ```json...``` 围栏，围栏内 {option, content}             → parse_method=fence_full
4b) LLM 套 ```json...``` 围栏，围栏内裸 ECharts option             → parse_method=fence_option
4c) LLM 套 ```json...``` 围栏，围栏内裸 ECharts + 围栏外有文字    → parse_method=fence_option
5)  以上都不满足                                               → 502 + raw_reply
```
