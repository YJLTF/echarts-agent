# AGENTS.md · 开发者指南

本文档面向希望在 `echarts-agent` 项目上进行开发、调试或定制的工程师。

---

## 环境

- **Python** ≥ 3.9
- **依赖**：`pip install -r requirements.txt`（Flask / flask-cors / pandas / openpyxl / langchain / langchain-core / langchain-openai / pydantic）
- **启动**：`python app.py`，默认端口 `8080`（可用 `PORT=` 覆盖）
- **首次使用**：先在 `/config` 配置 **Base URL / API Key / 模型名**，否则 `/api/chart` 返回 428
- **配置存储**：`app.db`（SQLite，`CREATE TABLE IF NOT EXISTS`）；已在 `.gitignore` 中

---

## 架构总览

```
┌─────────────────────────────────────────────────┐
│              1. app.py (HTTP 路由)               │
│    /api/parse  /api/chart  /api/chart/stream     │
│          run_chart_pipeline() 事件流              │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│              2. llm_client.py                    │
│   ChatOpenAIWrapper / get_llm_wrapper            │
│   call_llm / call_llm_raw / call_llm_stream      │
│   .structured(Pydantic) → with_structured_output │
│   pick_chart_type / build_chart_prompt           │
│   compute_column_stats                           │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│       3. output_parsers/schema.py                │
│   Pydantic 数据契约 + ThinkingStrategy 多态       │
│   • ChartGenerationResponse / DataUnderstanding  │
│     Response / ChartTypeRecommendation          │
│   • ThinkingStrategy 抽象 + 3 个具体策略         │
│   • ProviderConfig.thinking = ThinkingStrategy  │
│   • PipelineEvent (事件协议参考 schema)          │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│       4. prompts/  (ChatPromptTemplate 中心)      │
│   • DEFAULT_CHART_SYSTEM_PROMPT                  │
│   • data_understanding_prompt_template()         │
│   • chart_type_prompt_template()                 │
│   • build_chart_user_prompt(...)                 │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│       5. 数据解析 / 预处理 / 知识库               │
│   data_parser.py / data_preprocessing.py         │
│   knowledge.py / data_understanding.py           │
└─────────────────────────────────────────────────┘
```

**职责划分**：
- `app.py` 是唯一入口：路由、配置持久化、SSE 推送、6 阶段 pipeline
- `llm_client.py` 是 LLM 层的唯一抽象：Provider 嗅探、ChatOpenAI 封装、thinking 字段处理、prompt 构造工具
- `output_parsers/schema.py` 是数据契约：Pydantic 模型 + Thinking 策略多态
- `prompts/` 是 Prompt 模板中心：所有 system / user prompt 用 `ChatPromptTemplate` 统一管理
- `data_*` 模块各自负责一个数据阶段（解析 / 整理 / 预处理 / 知识库）

**核心设计原则**：
- **强类型优先**：LLM 调用尽量走 `with_structured_output(Pydantic)`，自动注入 JSON schema 说明、自动重试、自动校验
- **3 层降级**：structured → JsonOutputParser → 手写正则/栈扫描（保证老 LLM/代理还能跑）
- **多态 thinking**：每个 provider 一个 `ThinkingStrategy` 子类，扩展新 provider 只需加一个类
- **Prompt 解耦**：模板与业务代码分离，便于 A/B 测试、版本管理

**向后兼容原则**：
- `app.py` 的对外 API（`/api/chart` 等）保持不变
- `llm_client.py` 保留 `call_llm` / `call_llm_raw` / `call_llm_stream` / `get_llm_wrapper` 等旧接口
- `data_parser.py` / `data_preprocessing.py` / `knowledge.py` 的签名不变
- `_parse_structured_chart` 保留为 5 层手写 fallback（structured / JsonOutputParser 都失败时使用）
- `output_parsers/schema.py::ProviderConfig` 是 provider 策略的扩展点

---

## 核心模块详解

### 1. `output_parsers/schema.py` — Pydantic 数据模型 + Thinking 策略

| 类 | 用途 | 状态 |
|---|---|---|
| `ProviderConfig` | Provider 策略配置（含 `thinking: ThinkingStrategy` 多态） | **活跃使用**（被 `llm_client.py` 调用） |
| `ThinkingStrategy` | 抽象基类：把 `llm_thinking` 用户取值翻译成 LLM extra kwargs | **活跃使用** |
| `OpenAIStyleStrategy` | OpenAI / DeepSeek / DashScope 的 `reasoning_effort` 映射（off 时不发送） | **活跃使用** |
| `OllamaStyleStrategy` | Ollama 的 `reasoning_effort` 映射（off 时发 `"none"`） | **活跃使用** |
| `GLMStyleStrategy` | 智谱 GLM 的 `thinking` 字段映射（`{type: disabled/enabled}`） | **活跃使用** |
| `ColumnSchema` | 数据列元信息（name/type/role/description/unit） | 活跃使用（`DataUnderstandingResponse` 引用） |
| `DataUnderstandingResponse` | 大模型整理数据后的输出（columns + rows + summary + notes） | **活跃使用**（`data_understanding.py` 用 `with_structured_output` 调 LLM） |
| `ChartGenerationResponse` | 图表生成的双模式结构化输出（**option** 模式 + **code** 模式 + content） | **活跃使用**（`app.py` 用 `with_structured_output` 调 LLM） |
| `ChartTypeRecommendation` | 图表类型推荐（chart_type + reason） | **活跃使用**（`pick_chart_type` 用 `with_structured_output` 调 LLM） |
| `PipelineEvent` | Pipeline 事件对象（type / stage / status / content / data） | 协议文档化（运行时实际用裸 dict 产出） |

**`ChartGenerationResponse` 双模式**：

LLM 可以选择两种输出模式（`model_validator` 保证至少一个非空）：

- **option 模式**（旧）：`option: Dict[str, Any]` 是严格 JSON 配置，回调字段用字符串模板（`'{b}: {c}'`）
- **code 模式**（新，推荐）：`code: str` 是完整可执行 JS 代码段，含 `const option = {...}` 定义 + `chart.setOption(option)` 调用；option 内可使用**任意 ECharts 回调函数**（formatter / label.formatter / axisLabel.formatter 等），让渲染效果更灵活

校验策略：
- option 模式：Pydantic 强类型校验（`option: Dict[str, Any]`）
- code 模式：仅校验非空字符串（具体 JS 合法性由前端 sandbox iframe 验证）

前端 `renderResponse` 根据 `is_code_mode` 标志派发：
- `is_code_mode=True` → 把 `data.code` 发送到 `static/sandbox.html`（运行在 `sandbox="allow-scripts"` 的 null-origin iframe），沙箱里 `new Function` 执行，捕获 `chart.setOption(option)` 的 option，postMessage 回主页面渲染
- `is_code_mode=False` → 直接 `chart.setOption(data.option, true)`

**安全考量**：
- sandbox iframe 设 `sandbox="allow-scripts"`（无 `allow-same-origin`）→ null origin，无法访问主页面 cookie / localStorage
- 仍能执行 `fetch` / `WebSocket` / `localStorage`（iframe 自己的）→ 用户应使用自托管或可信 LLM
- 不建议把 sandbox 用于执行完全不可信的代码（不是本场景；LLM 输出由用户配置）
- 前端有 try/catch，sandbox 报错会回到主页面显示 fallback error

**`ThinkingStrategy` 多态用法**：

```python
from output_parsers.schema import ProviderConfig

cfg = ProviderConfig.for_provider("ollama")
# cfg.thinking = OllamaStyleStrategy 实例
# cfg.resolve_extra("medium") 直接返回 LLM 用的 extra kwargs

cfg.resolve_extra("medium")  # {"reasoning_effort": "medium"}
cfg.resolve_extra("off")      # {"reasoning_effort": "none"}

cfg = ProviderConfig.for_provider("glm")
# cfg.thinking = GLMStyleStrategy 实例
cfg.resolve_extra("high")   # {"thinking": {"type": "enabled"}}
cfg.resolve_extra("off")    # {"thinking": {"type": "disabled"}}
```

扩展新 provider：在 `output_parsers/schema.py` 加一个 `XxxStrategy(ThinkingStrategy)` 子类 + 在 `ProviderConfig.for_provider` 注册一行。

**`PipelineEvent` 协议参考**：

```python
# 实际运行时由 app.py 用裸 dict 产出（Pydantic 仅作协议文档化）：

{"type": "stage",   "stage": "<name>", "status": "start|done|skipped|error", ...}
{"type": "delta",   "content": "<token chunk>"}
{"type": "done",    "chart_type": "...", "option": {...}, "content": "...", ...}
{"type": "error",   "message": "...", "status": 502, "raw_reply": "..."}
```

### 2. `llm_client.py` — Provider 策略与 LLM 封装

| 名称 | 作用 |
|---|---|
| `ChatOpenAIWrapper` | LangChain `ChatOpenAI` 的包装类；`call_llm` / `call_llm_raw` / `call_llm_stream` / `structured` 四个入口；内部按 `(stream, frozenset(overrides))` 缓存底层 ChatOpenAI 实例避免重复构造 |
| `ChatOpenAIWrapper.structured(schema)` | 返回 `with_structured_output(schema)` 的 Runnable —— LLM 返回值即 Pydantic 对象 |
| `ChatOpenAIWrapper.to_lc_messages(msgs)` | OpenAI-style dict → LangChain 消息列表 |
| `ChatOpenAIWrapper._bind_common(...)` | 统一绑定 max_tokens / temperature / response_format / thinking 字段 |
| `get_llm_wrapper(cfg)` | 全局单例缓存，按 base_url + api_key + model 命中 |
| `get_provider_config(base_url, provider=None)` | 嗅探 Provider 并返回 `ProviderConfig` 对象 |
| `_sniff_provider(u)` | 从小写 Base URL 嗅探 provider 名称 |
| `_detect_provider(base_url)` | 旧接口兼容：返回 provider 名字符串 |
| `resolve_extra_kwargs(base_url, user_value, provider=None)` | 把 `off / low / medium / high` 解析为传给 LLM 的 extra kwargs |
| `_lc_messages_to_dicts(messages)` | LangChain 消息 → `[{"role","content"}]`（pick_chart_type 把模板渲染结果转给 fallback 路径用） |
| `pick_chart_type(cfg, prompt, data, hint)` | LLM 推荐图表类型（用 `with_structured_output(ChartTypeRecommendation)`）；hint 非空时直接返回；失败时 fallback 复用同一份 ChatPromptTemplate |
| `_pick_chart_type_fallback(cfg, messages)` | 兼容回退：直接发送已渲染好的消息，取首行 chart_type + 第二行理由 |
| `build_chart_prompt(prompt, data, chart_type, style_hint, knowledge)` | 拼装给主生成阶段的 user prompt（薄封装，调用 `prompts.build_chart_user_prompt`） |
| `compute_column_stats(rows, column_names)` | 数值列紧凑统计；>20 行才返回，>100 行追加 percentile |

### 3. `data_understanding.py` — LLM 数据理解

```python
from data_understanding import understand_data

result = understand_data(cfg, raw_preview, parsed, user_hint="")
# → {"columns": [{"name","type","role","description"}, ...],
#    "column_names": [...],
#    "rows": [...],
#    "count": int,
#    "summary": str,
#    "notes": str,
#    "understand_method": "llm|fallback",
#    "understand_error": str|None}
```

**内部**（3 层降级）：
1. **首选** `wrapper.structured(DataUnderstandingResponse).invoke(messages)` —— LangChain 自动注入 JSON schema 说明 + 失败重试
2. **降级** `JsonOutputParser().invoke(raw_reply)` —— 自由 JSON，宽容解析
3. **兜底** 手写栈扫描 + 尾随逗号修复（最坏情况）

校验与归一化（与旧版一致）：type ∈ {string, number, date, boolean}；role ∈ {category, value, time, series, label, ignore}；数值字段尝试去千分位 / 单位转 float

prompt 用 `prompts.data_understanding_prompt_template()`（`ChatPromptTemplate`）渲染。

### 3.5 `prompts/` — Prompt 模板中心

用 LangChain 的 `ChatPromptTemplate` 统一管理所有 system / user prompt，业务代码只 `format_messages(**params)`。

| 名称 | 作用 |
|---|---|
| `DEFAULT_CHART_SYSTEM_PROMPT` | 图表生成默认系统提示词（用户可在配置页覆盖） |
| `build_chart_user_prompt(prompt, data, chart_type, style_hint, knowledge)` | 构造图表生成阶段的 user prompt 文本（内部已挂预处理注记；data 含 `preprocess` 字段时自动追加精度提示） |
| `CHART_TYPE_SYSTEM` + `chart_type_prompt_template()` | 图表类型推荐的 system + human 模板 |
| `format_data_for_type_prompt(prompt, data, allowed_list)` | 准备图表类型推荐的 `format_messages` 入参 |
| `DATA_UNDERSTANDING_SYSTEM` + `DATA_UNDERSTANDING_USER_TEMPLATE` + `data_understanding_prompt_template()` | 数据整理的 system + human 模板 |

**为什么用 ChatPromptTemplate 而不是字符串拼装？**
- 模板与业务代码解耦，便于 A/B 测试、多版本管理
- `MessagesPlaceholder` 留出多轮对话位置（未来扩展）
- `partial()` 注入运行时变量（运行时构造的 `system_prompt` 字符串可作为 partial 变量）
- LangChain 的工具链能识别 ChatPromptTemplate 类型的 prompt（监控、可视化）

> **已删除**（2024 优化）：`_USER_PROMPT_TEMPLATE` 与 `chart_generation_prompt()` —— 旧版 ChatPromptTemplate 方案被 `build_chart_user_prompt` 字符串拼接取代（更直接、性能更好、无模板解析开销）。

### 4. `data_preprocessing.py` — 本地规则引擎

```python
from data_preprocessing import preprocess_data

new_data, info = preprocess_data(prompt, data)
# → (new_data, {"rules": [...], "applied": [...], "skipped": [...], "summary": "..."})
```

**支持的规则**（按 prompt 里出现顺序无关；按 `_RULE_PRIORITY` 生效）：
1. `round` / `round_col` —— 数值四舍五入到 N 位小数
2. `drop_null` —— 去除空值行
3. `dedup` —— 整行去重
4. `iqr_outlier` —— 1.5×IQR 之外的视为离群，整行删除
5. `group_sum` / `group_mean` —— 按某列分组聚合
6. `sort` —— 按某列升/降序
7. `top_n` —— 取最大/最小的前 N 行
8. `strip_thousands` —— 去掉字符串里的千分位逗号

中文数字（"两" / "十二" / "二十三"）由 `_normalize_cn_numbers` 自动归一化为阿拉伯数字，正则才认得 "2" / "12" / "23"。

### 5. `knowledge.py` — ECharts 知识库

```python
from knowledge import get_knowledge_for_type, search_knowledge

sections = get_knowledge_for_type("bar")
# → {"通用基础": "...", "tooltip": "...", "图表类型：bar": "...", "轴配置": "..."}

sections = search_knowledge("饼图")
```

**节流策略**：按 `CHART_USES_AXIS` 表只发相关章节，节省 15-25% token：
- `pie` / `funnel` / `sunburst` / `treemap` / `sankey` / `gauge` 不发 axis 章节
- `gauge` / `heatmap` / `candlestick` / `boxplot` 不发 legend 章节

### 6. `app.py` — Flask 路由 + Pipeline

```
URL 路由
├── GET  /                       → render chat.html
├── GET  /config                 → render chat.html + open config modal
├── GET  /api/config             → 读配置（API Key 掩码）
├── POST /api/config             → 存配置（Key 留空不覆盖）
├── POST /api/config/test        → 测试连接（body 可覆盖 DB）
├── POST /api/parse              → 解析文件/文本；use_llm=1 时调 LLM 整理
├── GET  /api/knowledge?q=|?chart_type= → 查知识库
├── POST /api/chart              → 非流式主生成
├── POST /api/chart/stream       → 流式主生成（SSE）
├── GET  /api/chats              → 对话列表
└── GET  /api/chats/<id>/messages → 指定对话消息列表

run_chart_pipeline() 阶段
├── prepare      数据准备
├── understand   智能数据整理（复用 / 调 LLM / 跳过）
├── preprocess   本地规则预处理
├── pick_type    选择图表类型（LLM / 用户的 hint）
├── generate     主生成（流式或一次性；response_format=json_schema，双模式）
└── parse        结构化输出解析（Pydantic 优先 + 5 层手写兜底）
```

**Pydantic 优先的 5 层兜底**（`app._parse_chart_response`）：
1. `ChartGenerationResponse.model_validate_json(raw)` → `(option, code, content)` → `primary`
2. 顶层 `json.loads` 是合法 JSON 且 `option` + `content` 同时存在 → `primary`（手写 fallback 路径）
3. 顶层是裸 ECharts option（series/title/...）→ 看是否有顶层 `content` 字段，抽出 → `in_option` / `primary_bare`
4. 扫 ``` ``` ```json``` ``` 围栏 → 围栏内是 `{option, content}` → `fence_full`
5. 围栏内是裸 ECharts option + 围栏外文字当 content → `fence_option` / `fence_in_option`
6. 还不行 → 502 + raw_reply

非 `primary` / `primary_bare` 时前端顶部显示「⚠ LLM 偏离 schema」红色徽章。
`is_code_mode=True` 时额外显示「⚡ Code 模式」标签，提示当前 option 来自 sandbox 执行的 JS 代码。

---

## Provider 推理深度策略表

| Provider | 检测依据 | `off` | `low / medium / high` | 字段 |
|---|---|---|---|---|
| **openai** | 默认；DeepSeek / DashScope | 不发送 | `{"reasoning_effort": value}` | `reasoning_effort` |
| **ollama** | `base_url` 含 `:11434` 或 `/ollama` | `{"reasoning_effort": "none"}` | `{"reasoning_effort": value}` | `reasoning_effort` |
| **glm**（智谱） | `bigmodel.cn` / `zhipu` | `{"thinking": {"type": "disabled"}}` | `{"thinking": {"type": "enabled"}}` | `thinking` |

注册在 `output_parsers/schema.py::ProviderConfig.for_provider` 一张表里，新增 provider 只需加一个分支。

---

## 修改 Prompt 的注意事项

### 约束性提示

`data_understanding.py` 的 `SYSTEM_PROMPT` 末尾要求**只输出一个 JSON 对象**。这个约束保证解析器能提取出 `columns` / `rows`。如果要改 prompt，务必保持类似的约束。

`app.py::_DEFAULT_SYSTEM_PROMPT` 包含硬约束（首末字符、JSON schema 形状、禁用 JS 函数等）。改时需要同步保证：
- 解析器对 LLM 偏离时的兜底仍有效
- 主生成走 `json_schema` 严格模式依然能生效

---

## 添加新 Provider 的步骤

1. 在 `output_parsers/schema.py:ProviderConfig.for_provider` 加一个新的 `name` 分支
2. 在 `llm_client.py::_sniff_provider` 的 URL 嗅探规则中添加对应检测（或显式传 `provider=`）
3. 验证 `resolve_extra_kwargs(url, 'off')` 和 `resolve_extra_kwargs(url, 'high')` 的返回值

---

## 添加新预处理规则的步骤

1. 在 `data_preprocessing.py::_parse_rules` 加正则
2. 在 `_apply_rule` 加对应 `t == "..."` 分支
3. 在 `_RULE_PRIORITY` 中插入新规则的优先级位置
4. 触发文档（README.md "数据预处理" 段）同步更新

---

## 添加新图表类型（如 3D 柱状图）

1. 在 `knowledge.py` 增加 `BAR3D = """..."""` 知识片段，登记到 `TYPE_MAP` / `TYPE_BODY`
2. 在 `llm_client.py::pick_chart_type` 的 `system` 列表与 `allowed` 白名单中加新类型
3. 在前端 `templates/chat.html` 的 `<select id="typeSel">` 中加入对应选项
4. 验证 `pick_chart_type(...)` 和 `generate_chart(...)` 输出正确

---

## 调试技巧

### 模块级单元测试（不启动 Flask）

```python
from output_parsers.schema import (
    ProviderConfig, PipelineEvent, ChartGenerationResponse,
    DataUnderstandingResponse,
)
from llm_client import get_provider_config, resolve_extra_kwargs
from data_preprocessing import preprocess_data
from data_understanding import understand_data
from knowledge import get_knowledge_for_type
```

### 端到端测试（Flask test client）

```python
import app
client = app.app.test_client()

# 解析数据
resp = client.post("/api/parse",
                   data={"text": "月份,销售额\nJan,100\nFeb,200"},
                   content_type="multipart/form-data")

# 生成图表（非流式）
resp = client.post("/api/chart", json={
    "prompt": "画柱状图",
    "data": resp.json,
})

# 生成图表（流式 SSE）
resp = client.post("/api/chart/stream", json={"prompt": "..."}, stream=True)
for line in resp.response:
    print(line.decode())
```

### 快速验证 Pydantic 模型

```python
# 合法的 ChartGenerationResponse
ChartGenerationResponse(
    option={"title": {"text": "..."}, "xAxis": {"data": ["Jan"]}},
    content="这是图表解读",
)

# option 必须是 dict，以下会抛出异常
try:
    ChartGenerationResponse(option="字符串", content="xxx")
    assert False
except Exception:
    print("Pydantic 正确拒绝了非法 option")
```

### 查看数据库

```bash
python -c "import sqlite3; c = sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"
```

### 调试 Provider 策略

```python
from llm_client import resolve_extra_kwargs
print(resolve_extra_kwargs('http://localhost:11434/v1', 'off'))
print(resolve_extra_kwargs('https://open.bigmodel.cn/api/paas/v4', 'high'))
print(resolve_extra_kwargs('https://api.deepseek.com/v1', 'medium'))
```

---

## 测试矩阵（parse_method 降级）

```
1) Pydantic 强类型校验 {option?, code?, content}          parse_method = primary, is_code_mode = bool(code)
2) json.loads 顶层就是 {option, content}                  parse_method = primary（fallback 路径）
3) 顶层合法 JSON 但缺字段（裸 ECharts option，干净）        parse_method = primary_bare
4) 顶层裸 ECharts option + 顶层有 content                  parse_method = in_option
5) 扫 ```json``` 围栏 → 围栏里 {option, content}           parse_method = fence_full
6) 围栏里是裸 ECharts option + 围栏外文字当 content        parse_method = fence_option
7) 围栏 + content-in-option 变种                          parse_method = fence_in_option
8) 还不行 → 502 + raw_reply
```

非 `primary` / `primary_bare` 时前端顶部显示「⚠ LLM 偏离 schema」红色徽章。
`is_code_mode=True` 时额外显示「⚡ Code 模式」标签，提示当前 option 来自 sandbox 执行的 JS 代码。

---

## 引入新依赖的规则

✅ **允许**：`langchain-core`、`langchain-openai`、`langchain-community`、`pydantic`（已装）

❌ **禁止**：`langgraph` / `langsmith` / 与 LangChain Agent 深度耦合的包

❌ **禁止**：破坏现有 `/api/chart` 接口签名的变更

---

## 项目结构

```
echarts-agent/
├── app.py                      # Flask 入口：路由 / 配置持久化 / pipeline / SSE
├── llm_client.py               # LLM 调用封装：LangChain ChatOpenAI + Provider 策略
├── data_parser.py              # Excel / CSV / JSON / 文本统一解析
├── data_understanding.py       # LLM 数据理解与整理
├── data_preprocessing.py       # 本地规则预处理引擎（10 种规则）
├── knowledge.py                # ECharts 知识库（15 种图表配置指导）
├── output_parsers/
│   └── schema.py               # Pydantic 数据模型：ProviderConfig / PipelineEvent 等
├── prompts/
│   └── __init__.py             # ChatPromptTemplate 中心（chart_generation / chart_type / data_understanding）
├── requirements.txt            # Python 依赖
├── scripts/
│   └── download_vendor.py      # 首次部署：拉取 echarts + dark 主题到 static/vendor
├── templates/
│   └── chat.html               # 可视化对话页
└── static/
    ├── app.css                 # 样式（暗色代码区 + 浅色主题）
    ├── app.js                  # 流式 SSE 客户端 + ECharts 渲染 + 配置弹窗 + sandbox 调度
    ├── sandbox.html            # null-origin iframe，沙箱执行 LLM 生成的 JS 代码
    └── vendor/echarts/         # 本地 ECharts + dark 主题
```

---

## 代码优化笔记

最近一次清理（2025）做了哪些事：

### Prompt 清理

- **`DEFAULT_CHART_SYSTEM_PROMPT`** 修复了一处策略矛盾：原 prompt 在【硬约束】里同时写「JSON 首字符必须是 `{{`、末字符必须是 `}}`」与「绝对禁止把 `{` / `}` 写成 `{{` / `}}`」—— 自相矛盾，也是 LLM 受 f-string 训练数据副作用后输出 `{{` 的根因。现已统一为单 `{` / `}`，仅在 ❌ 反例里保留 `{{` / `}}` 作为「不要这样做」的视觉对照。
- 删除重复的「【质量约束】」标题（出现两次）。
- 合并 / 重排硬约束为 8 条；formatter 规则统一收口到「option 模式：字符串模板；code 模式：完整 JS 函数」。
- `build_chart_user_prompt` 删掉与新 system prompt 重复的「禁止 JS 函数字面量」段落。

### 解析器清理

- `app._parse_chart_response` 路径 1 原本用 `if candidate == raw or candidate != raw`（恒为真），会无意义地跑两遍 Pydantic；现改为「raw 失败再试 unescape 后的版本（仅在二者不同时）」。
- `_parse_structured_chart` 的 docstring 之前写的方法名（`fence_fence_full` / `fence_fence_option` / `fence_fence_in_option`）与实际不符，已纠正为 `fence_full` / `fence_option` / `fence_in_option`。
- 前端 `parseBadge` 现在把 `primary_bare` 与 `primary` 一视同仁（不显示「⚠ LLM 偏离 schema」徽章）—— `primary_bare` 意味着 LLM 直接输出裸 ECharts option，结构上没毛病，只是没用 `{option, content}` 包装而已。

### Schema 清理

- `output_parsers/schema.py` 删除了未使用的 `ThinkingStrategy.field_name` ClassVar 与 `ProviderConfig.thinking_field` 属性（全代码库无 read site）。
- `PipelineEvent` 文档中已删除不存在的 `chart_type` / `understanding` 事件类型（运行时实际只在 `stage` 事件里附带 `chart_type` / `understanding` 字段）。

### 前端清理

- 修 CSS bug：`.configModal textarea::-webkit-scrollbar` 不会匹配任何元素（modal 实际是 `id="configModal"`），改为 `#configModal textarea`。
- `markAllStagesDone` 不再硬编码 6 个 stage 名，改用 `STAGE_KEYS` 常量。
- `renderDataPreview` / `renderResponse` 把 LLM 输出的字符串（`summary` / `notes` / `error` / `type_reason`）经 `escapeHtml` 后再注入 innerHTML，关闭 XSS 路径。
- chat.html 给 prompt / text 输入框加 `aria-label`。

### 其它

- `data_preprocessing.py::_group_aggregate` 里 `verb` 重复计算已合并。
- `data_understanding.py::_llm_understand` 删了无用的 `structured_err` 局部变量。
- 无测试脚本需要删除（仓库历史里没有 `_test.py` / `tests/` 目录；临时验证用脚本只在 `/tmp` 跑过，未提交）。
