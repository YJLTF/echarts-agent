# AGENTS.md · 开发者指南（架构重构 v2）

本项目基于 Flask + LangChain 的 **分层架构**。
本文档面向希望在 `echarts-agent` 上开发、调试、或定制功能的工程师。

---

## 1. 环境

- **Python** ≥ 3.9
- **依赖**：`pip install -r requirements.txt`（内含 Flask / langchain / langchain-core / langchain-openai / pydantic）
- **启动**：`python app.py`，默认端口 `8080`
- **首次使用**：先在 `/config` 配置 **Base URL / API Key / 模型名**，否则 `/api/chart` 返回 428
- **配置存储**：`app.db`（SQLite，`CREATE TABLE IF NOT EXISTS`）；不要提交到仓库

---

## 2. 架构总览

**层次**（从顶层到底层）：

```
┌─────────────────────────────────────────────────┐
│              1. app.py (HTTP 路由)               │
│    /api/parse  /api/chart  /api/chart/stream     │
│          run_chart_pipeline() 事件流              │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│             2. chains/* (LangChain Chain)         │
│   ┌──────────────────────────────────────────┐   │
│   │ chains/understanding.py                  │   │
│   │   └─ StructuredLLMChain(DataUnderstanding)│  │
│   │ chains/chart_generation.py               │   │
│   │   └─ StructuredLLMChain(ChartGeneration) │   │
│   │ chains/base.py                            │   │
│   │   └─ build_llm / StructuredLLMChain      │   │
│   └──────────────────────────────────────────┘   │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│           3. prompts/__init__.py (注册中心)       │
│   data_understanding / chart_type / chart_gen    │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│    4. output_parsers/schema.py (Pydantic 模型)   │
│   ColumnSchema / DataUnderstandingResponse       │
│   ChartGenerationResponse / ChartTypeRecommendation│
│   ProviderConfig / PipelineEvent                  │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│        5. llm_client.py (ChatOpenAIWrapper)      │
│   ┌ get_provider_config() → ProviderConfig       │
│   └ resolve_extra_kwargs() → reasoning_effort /  │
│     thinking 字段                                │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│      6. 数据解析 / 预处理 / 知识库（根目录）        │
│   data_parser.py / data_preprocessing.py         │
│   knowledge.py / data_understanding.py           │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│     7. memory/chat_memory.py — 进程内对话记忆      │
│   ChatMemory / LangChainChatMessageHistory       │
└─────────────────────────────────────────────────┘
```

**向后兼容原则**：
- `app.py` 的对外 API（`/api/chart` 等）保持不变
- `llm_client.py` 保留 `call_llm` / `call_llm_raw` / `call_llm_stream` 等旧接口
- `data_parser.py` / `data_preprocessing.py` / `knowledge.py` 的签名不变
- 新增模块（`chains/*` / `output_parsers/schema.py` / `prompts/__init__.py` / `memory/*`）都是可选扩展点

---

## 3. 核心模块详解

### 3.1 `output_parsers/schema.py` — Pydantic 数据模型

替代手写 JSON schema + 手写解析逻辑的双层方案：

| 类 | 用途 |
|---|---|
| `ColumnSchema` | 数据列元信息（name/type/role/description/unit） |
| `DataUnderstandingResponse` | 大模型整理数据后的输出（columns + rows + summary + notes） |
| `ChartGenerationResponse` | 图表生成的结构化输出（**option: dict** + **content: str**）；**Pydantic 级别的类型校验**会拒绝把字符串填进 `option` 字段 |
| `ChartTypeRecommendation` | 图表类型推荐（chart_type + reason） |
| `ProviderConfig` | Provider 策略配置（name / thinking_field / disabled_value / effort_values） |
| `PipelineEvent` | Pipeline 事件对象（stage/delta/done/error） |

**`ProviderConfig` 使用方法**：

```python
from output_parsers.schema import ProviderConfig

cfg = ProviderConfig.for_provider("ollama")
# cfg.thinking_field = "reasoning_effort"
# cfg.thinking_disabled_value = "none"

extra = cfg.resolve_extra("medium")  # {"reasoning_effort": "medium"}
extra = cfg.resolve_extra("off")     # {"reasoning_effort": "none"}

for glm:
    cfg = ProviderConfig.for_provider("glm")
    cfg.resolve_extra("high")  # {"thinking": {"type": "enabled"}}
    cfg.resolve_extra("off")   # {"thinking": {"type": "disabled"}}
```

**`PipelineEvent` 工厂方法**：

```python
from output_parsers.schema import PipelineEvent

PipelineEvent.create_stage("generate", "start", "开始生成")  # type="stage"
PipelineEvent.create_delta("一个 ")                             # type="delta"
PipelineEvent.create_done("bar", option={...}, content="...",
                          raw_reply="...", parse_method="primary")  # type="done"
PipelineEvent.create_error("LLM 不可用", status=503)             # type="error"
```

### 3.2 `llm_client.py` — Provider 策略与 LLM 封装

**主要类/函数**：

| 名称 | 作用 |
|---|---|
| `ChatOpenAIWrapper` | LangChain `ChatOpenAI` 的包装类；`call_llm` / `call_llm_raw` / `call_llm_stream` 三个入口 |
| `get_llm_wrapper(cfg)` | 全局单例缓存，按 base_url + api_key + model 命中 |
| `get_provider_config(base_url, provider=None)` | 嗅探 Provider 并返回 `ProviderConfig` 对象 |
| `resolve_extra_kwargs(base_url, user_value, provider=None)` | 把用户输入的 `off / low / medium / high` 解析为传给 LLM 的 extra kwargs |

### 3.3 `chains/base.py` — LangChain Chain 构建器

核心类 `StructuredLLMChain` 提供一个**声明式的 Chain 装配**：

```python
from chains.base import StructuredLLMChain
from output_parsers.schema import ChartGenerationResponse

chain = StructuredLLMChain(
    cfg={"base_url": "...", "api_key": "...", "model": "..."},
    system_prompt="你是专业的 ECharts 图表生成助手",
    output_model=ChartGenerationResponse,
)

# invoke（非流式）
result = chain.invoke(user_input)  # 返回 ChartGenerationResponse 或 dict

# stream（流式）
for chunk in chain.stream(user_input):
    print(chunk, end="")
```

内部逻辑：
1. 优先尝试 `ChatOpenAI.with_structured_output(PydanticModel)`（JSON Schema 严格模式）
2. 失败时降级为普通调用 + 手写 JSON 解析
3. 还失败时返回原始字符串

### 3.4 `chains/understanding.py` — 数据理解链

```python
from chains.understanding import build_data_understanding_chain, invoke_data_understanding

# 直接调用
parsed_result = invoke_data_understanding(cfg, parsed, user_hint, raw_text)
# → {"columns", "column_names", "rows", "count", "summary", "notes",
#    "understand_method", "understand_error"}
```

### 3.5 `chains/chart_generation.py` — 图表生成链

```python
from chains.chart_generation import build_chart_generation_chain, generate_chart

# 非流式
option, content, parse_method_or_error = generate_chart(
    cfg, prompt, data, chart_type="bar",
    style_hint={}, preprocess_info={},
)

# 流式
for chunk in generate_chart_stream(cfg, prompt, data, chart_type="bar"):
    print(chunk, end="")
```

### 3.6 `prompts/__init__.py` — Prompt 注册中心

```python
from prompts import (
    DATA_UNDERSTANDING_SYSTEM,  # data_understanding 的 system prompt
    build_data_understanding_input,  # 构造 user prompt 的 template 填充
    CHART_GEN_SYSTEM,           # chart_generation 的 system prompt
    build_chart_user_prompt,    # 构造图表生成 user prompt
)
status()  # {'data_understanding': True, 'chart_type': True, 'chart_generation': True}
```

**添加一个新 Prompt 的流程**：
1. 在 `prompts/<新模块>.py` 定义 `SYSTEM_PROMPT` 常量与 `build_xxx_prompt()` 函数
2. 在 `prompts/__init__.py` 用 try/except 包裹导入并 re-export
3. 在需要使用的 Chain 里用 `StructuredLLMChain(system_prompt=...)` 接入

### 3.7 `memory/chat_memory.py` — 进程内对话记忆

```python
from memory.chat_memory import ChatMemory, LangChainChatMessageHistory

# 简易 API（与旧代码兼容）
from memory.chat_memory import get_memory, add_user_message, add_ai_message, clear_memory

# LangChain 风格
history = LangChainChatMessageHistory(session_id="default", max_messages=20)
history.add_user_message("分析销售数据")
history.add_ai_message("已分析销售数据，共 12 个月...")
for msg in history:       # 可迭代
    print(msg.role, msg.content)
messages = history.messages        # List[SimpleMessage]
raw_list = history.to_raw_list()   # 可直接传给 LLM
history.clear()
```

---

## 4. Provider 推理深度策略表

| Provider | 检测依据 | `off` | `low / medium / high` | 字段 |
|---|---|---|---|---|
| **openai** | 默认；DeepSeek / DashScope | 不发送 | `{"reasoning_effort": value}` | `reasoning_effort` |
| **ollama** | `base_url` 含 `:11434` 或 `/ollama` | `{"reasoning_effort": "none"}` | `{"reasoning_effort": value}` | `reasoning_effort` |
| **glm**（智谱） | `bigmodel.cn` / `zhipu` | `{"thinking": {"type": "disabled"}}` | `{"thinking": {"type": "enabled"}}` | `thinking` |

在 `llm_client.py` 中：
- `ChatOpenAIWrapper._build_payload()` 内部调用 `resolve_extra_kwargs()`
- `llm_client.call_llm_raw()` / `_build_llm()` 中也使用 `resolve_extra_kwargs()`

---

## 5. 修改 Prompt 的注意事项

### 5.1 约束性提示

`data_understanding.py` 的 `SYSTEM_PROMPT` 末尾要求**只输出一个 JSON 对象**。
这个约束保证 `parse_chart_response` 能提取出 `columns` / `rows`。
如果你要改 prompt，务必保持类似的约束。

### 5.2 Prompt 与解析器配对

- `chains/understanding.py` 的解析逻辑与 `output_parsers/schema.py:DataUnderstandingResponse` 绑定
- `chains/chart_generation.py` 的解析逻辑与 `ChartGenerationResponse` 绑定
- 改解析器 → 同步检查 prompt 约束

---

## 6. 添加新 Provider 的步骤

1. 在 `output_parsers/schema.py:ProviderConfig.for_provider` 加一个新 `name` 判断
2. 在 `llm_client.py:get_provider_config` 的 URL 嗅探规则中添加对应检测（或显式传 `provider=`）
3. 写一个简单测试：`assert 'xxx' in resolve_extra_kwargs(url, 'high')`

---

## 7. 调试技巧

### 7.1 模块级单元测试（不启动 Flask）

```python
from output_parsers.schema import (
    ProviderConfig, PipelineEvent, ChartGenerationResponse,
    DataUnderstandingResponse,
)
from llm_client import get_provider_config, resolve_extra_kwargs
from chains.base import StructuredLLMChain
```

### 7.2 端到端测试（Flask test client）

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

### 7.3 快速验证 Pydantic 模型

```python
# 验证好的 ChartGenerationResponse
ChartGenerationResponse(
    option={"title": {"text": "..."}, "xAxis": {"data": ["Jan"]}},
    content="这是图表解读",
)

# 拒绝非法字段（option 必须是 dict）
try:
    ChartGenerationResponse(option="字符串", content="xxx")
    assert False
except Exception:
    print("Pydantic 正确拒绝了非法 option")
```

### 7.4 查看数据库

```bash
python -c "import sqlite3; c = sqlite3.connect('app.db'); print(list(c.execute('SELECT * FROM config')))"
```

### 7.5 调试 Provider 策略

```python
from llm_client import resolve_extra_kwargs
print(resolve_extra_kwargs('http://localhost:11434/v1', 'off'))   # ollama 关闭
print(resolve_extra_kwargs('https://open.bigmodel.cn/api/paas/v4', 'high'))  # glm 启用
print(resolve_extra_kwargs('https://api.deepseek.com/v1', 'medium'))  # openai 中等
```

---

## 8. 测试矩阵（parse_method / response_format 降级）

```
1a) ChatOpenAI.with_structured_output → Pydantic对象        parse_method=structured
1b) 降级：JSON Schema response_format + json.loads          parse_method=primary
2a) 降级：普通 response_format=json_object + json.loads      parse_method=primary
2b) 降级：普通 response_format=json_object + content-in-option  parse_method=in_option
3a) 无 response_format + LLM 返回裸 ECharts option          parse_method=in_option
3b) 无 response_format + LLM 返回 {option,content}          parse_method=primary
4a) LLM 套 ```json 围栏，内部完整 {option,content}           parse_method=fence_full
4b) LLM 套 ```json 围栏，内部仅 ECharts option             parse_method=fence_option
5)  以上都不满足 → 502 + raw_reply
```

---

## 9. 引入新依赖的规则

✅ **允许**：`langchain-core`、`langchain-openai`、`langchain-community`、`pydantic`（已装）
❌ **禁止**：`langgraph` / `langsmith` / 与 LangChain Agent 深度耦合的包
❌ **禁止**：破坏现有 `/api/chart` 接口签名的变更

---

## 10. 项目结构参考

```
echarts-agent/
├── app.py                              # Flask 路由 + Pipeline
├── llm_client.py                       # ChatOpenAIWrapper + Provider 策略
├── data_parser.py                      # 解析 Excel/CSV/JSON/文本
├── data_understanding.py               # LLM 数据理解（保留旧实现）
├── data_preprocessing.py               # 本地规则预处理
├── knowledge.py                        # ECharts 知识库
├── requirements.txt                    # 依赖清单
├── output_parsers/
│   ├── __init__.py
│   ├── chart_parser.py                 # 5 层兜底 JSON 解析
│   └── schema.py                       # Pydantic 数据模型
├── prompts/
│   ├── __init__.py                     # Prompt 注册中心
│   ├── data_understanding.py           # 数据理解 system + user prompt
│   ├── chart_type.py                   # 图表类型选择 prompt
│   └── chart_generation.py             # 图表生成 prompt
├── chains/
│   ├── __init__.py
│   ├── base.py                         # StructuredLLMChain + build_llm
│   ├── understanding.py                # 数据理解 Chain
│   └── chart_generation.py             # 图表生成 Chain
├── memory/
│   ├── __init__.py
│   └── chat_memory.py                  # ChatMemory + LangChainChatMessageHistory
├── agents/
│   └── dataviz_agent.py                # 简化版 Agent（不依赖 tool-calling）
├── templates/
│   └── chat.html                       # 可视化对话页
└── static/
    ├── app.css / app.js
    └── vendor/echarts/                  # 本地化 ECharts + dark 主题
```

---

## 11. 典型开发路径

### 路径 A：新增一个图表类型（例如 3D 柱状图）

1. 在 `knowledge.py` 增加 `BAR_3D` 知识片段
2. 在 `prompts/chart_generation.py` 的 user prompt 中追加 `chart_type="bar3d"` 的分支
3. 验证前端下拉框的 `chart_type` 能透传到 `run_chart_pipeline`
4. 写测试：`generate_chart(cfg, "3D柱状图", data, chart_type="bar3d")`

### 路径 B：新增一个 Provider（例如 Anthropic Claude）

1. 在 `output_parsers/schema.py:ProviderConfig.for_provider("claude")` 定义：
   - `thinking_field = "thinking"`（或 `reasoning_effort`，取决于 Claude API）
   - `thinking_disabled_value = None`
2. 在 `llm_client.py:get_provider_config` 的 URL 嗅探中加一条：含 `anthropic` → `"claude"`
3. 构造几个请求，验证 `resolve_extra_kwargs(url, "off")` 返回空 dict，`"high"` 返回 `{"thinking": "high"}`

### 路径 C：重构现有手写 LLM 调用为 StructuredLLMChain

1. 定位 `app.py` 中 `call_llm_raw(...)` 的调用点
2. 把 `user_prompt` 字符串变成 `StructuredLLMChain(cfg, system_prompt, ChartGenerationResponse)`
3. `chain.invoke(user_text)` 直接拿到 Pydantic 对象，省去手写解析
4. 在 CI 中保持旧 API 兼容性测试：原 `run_chart_pipeline` 的事件结构不变
