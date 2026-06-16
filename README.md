# ECharts Agent · 可视化对话

一个基于 Flask + 任意 OpenAI 兼容 LLM 的 ECharts 图表生成助手。上传数据（Excel / CSV / JSON / 文本），用自然语言描述需求，即可生成交互式图表，并导出自包含的独立 HTML。

## 核心能力

- **自然语言生成图表**：用中文描述需求，模型自动选择图表类型、生成 ECharts 配置、完成渲染
- **多格式数据支持**：Excel（xlsx/xls，支持多 sheet）、CSV、JSON、纯文本（TSV，空格分隔等）
- **LLM 智能整理数据**：对不规范数据（多级表头、带单位、千分位、合并单元格、合计行等）自动清洗与列类型推断
- **本地规则预处理**：prompt 中的自然语言指令（"保留 2 位小数"、"前十"、"按月份分组求和"等）由本地规则引擎在 50ms 内完成，不消耗 LLM token
- **结构化输出**：主生成走 `json_schema` 严格模式；LLM 偏离 schema 时自动降级（围栏 → 裸 ECharts option → content-in-option），确保鲁棒
- **推理深度控制**：支持 reasoning_effort（DeepSeek/OpenAI/o1）、thinking（GLM-4.5+）、reasoning（Ollama）字段，按 provider 自动适配
- **流式生成（SSE）**：逐 token 推送给前端，图表下方有 6 阶段进度面板（准备 → 整理 → 预处理 → 选类型 → 生成 → 解析）
- **ECharts 知识库**：内置 15 种图表类型的配置指导，按类型裁剪后注入 prompt，节省 15-25% token
- **多轮对话记忆**：基于 `memory/chat_memory.py` 的进程内存储，会话级上下文保持
- **零隐私上传**：API Key 仅存本地 `app.db`；所有请求只发往你在配置页填写的 Base URL
- **内网可部署**：所有 JS 资源本地化，无 CDN 依赖

## 项目结构

```
echarts-agent/
├── app.py                  # Flask 入口：路由 / 配置持久化 / pipeline / SSE
├── llm_client.py           # LLM 调用封装：urllib 兼容层 + LangChain ChatOpenAI + Provider 策略
├── data_parser.py           # Excel / CSV / JSON / 文本统一解析
├── data_understanding.py    # LLM 数据理解与整理
├── data_preprocessing.py    # 本地规则预处理引擎
├── knowledge.py             # ECharts 知识库（15 种图表配置指导）
├── output_parsers/
│   ├── chart_parser.py       # ECharts option 5 层解析兜底
│   └── schema.py             # Pydantic 数据模型：ColumnSchema / DataUnderstandingResponse /
│                             #   ChartGenerationResponse / ProviderConfig / PipelineEvent
├── prompts/
│   ├── __init__.py          # Prompt 注册中心
│   ├── data_understanding.py
│   ├── chart_type.py
│   └── chart_generation.py
├── chains/
│   ├── __init__.py
│   ├── base.py              # StructuredLLMChain + build_llm 工厂
│   ├── understanding.py      # 数据理解 Chain
│   ├── chart_generation.py   # 图表生成 Chain
│   └── pipeline.py           # 6 阶段流水线
├── tools/
│   ├── knowledge.py
│   ├── preprocessor.py
│   └── chart_selector.py
├── agents/
│   └── dataviz_agent.py
├── memory/
│   ├── __init__.py
│   └── chat_memory.py       # ChatMemory + LangChainChatMessageHistory
├── requirements.txt          # Python 依赖
├── templates/
│   └── chat.html            # 可视化对话页
└── static/
    ├── app.css
    ├── app.js
    └── vendor/echarts/      # 本地 ECharts + dark 主题
```

运行时在根目录生成 `app.db`（SQLite），已在 `.gitignore` 中忽略。

## 快速开始

### 环境要求

- Python ≥ 3.9
- 浏览器（Chrome / Edge / Firefox 较新版本）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python app.py
# 默认端口 8080，可用 PORT 环境变量覆盖：
PORT=8000 python app.py
```

### 配置 LLM

1. 打开 http://127.0.0.1:8080/config
2. 填写 **Base URL**（如 `https://api.deepseek.com/v1` / `https://dashscope.aliyuncs.com/compatible-mode/v1` / `http://localhost:11434/v1` 等）
3. 填写 **API Key**
4. 填写 **模型名**（如 `deepseek-chat` / `qwen-plus` / `gpt-4o-mini` / `glm-4.5-air` 等）
5. 可选：自定义 System Prompt、Temperature、Max Tokens、**推理深度**（`off` / `low` / `medium` / `high`，仅对推理模型生效）
6. 点「🔌 测试连接」确认后再「💾 保存」

### 使用流程

1. **上传或粘贴数据**：支持 xlsx/xls/csv/json/txt；多 sheet xlsx 会弹出选择器
2. **（可选）勾选「🧠 用 LLM 智能整理数据」**：对不规范数据进行清洗
3. **输入需求**，例如「用月份作 X 轴、销售额作 Y 轴，画一个柱状图并带圆角」
4. 点「✨ 生成图表」，进度面板展示 6 个阶段
5. 图表下方有 4 个 tab：**文字解释 / option JSON / JS 代码 / 原始回复**；每个 tab 右上角有「📋 复制」
6. 右上角「📥 导出代码」下载**完全脱机的独立 HTML**（自带 ECharts + dark 主题，可双击离线打开）

### 数据预处理

在需求里写处理指令即可，例如：
- `保留 2 位小数` → 四舍五入
- `去除空值` / `去重` → 过滤
- `按月份分组求和` / `按月份分组求平均` → 聚合
- `前 10 大` / `降序排列` → 排序截取
- `去除异常值` → IQR 1.5× 整行删除

预处理由本地规则引擎完成（< 50ms），不消耗 LLM token。

## 核心设计

### 数据解析：双层架构

**第一层**：`data_parser.py` 纯代码解析（pandas / csv / json / 空格嗅探），搞定 90% 标准数据。

**第二层**：`data_understanding.py`（可选）：
- 触发方式：解析时勾选「🧠 用 LLM 智能整理」，或生成时勾选「🧠 生成时整理」
- LLM 接收：原始文本片段 + 代码解析草稿（列名 + 前几行 + 列统计摘要）
- LLM 输出：严格 JSON `{columns:[{name,type,role,description}], rows, summary, notes}`
- 失败时**自动回退**到代码解析结果，不会阻塞

### 图表生成 Pipeline（6 阶段）

```
用户 prompt + data
  ├─ 1) 数据准备              瞬时
  ├─ 2) 智能数据整理          复用 / 调一次 LLM / 跳过
  ├─ 3) 数据预处理（本地）    < 50ms 规则改写
  ├─ 4) 选择图表类型          调一次 LLM（可被下拉框 hint 覆盖）
  └─ 5) 主生成                流式调用 → 解析 → 自动修正
                               ↓
                          前端 echarts.setOption(option)
```

- **阶段 2 触发条件**：数据已有 `understand_method=llm`（解析时已整理）→ 直接复用；本次请求显式要求 → 调 LLM；都没有 → 跳过
- **阶段 3**：从 prompt 识别支持的规则（round / drop_null / group_sum / top_n / sort 等），无规则则跳过
- **阶段 4**：用户在下拉框选了类型则跳过 LLM 推荐
- **阶段 5**：主生成走 `json_schema` 严格模式 `response_format`，强制 `{option, content}` 形状；HTTP 400/422 时自动降级到 `json_object`，再降级到不带 `response_format`

### 结构化输出：Pydantic 优先 + 5 层兜底

LLM 可能不响应 `response_format`、可能套 ` ```json ``` ` 围栏、可能把 `content` 塞进 ECharts option 内部。解析器按此顺序兜底：

```
1) with_structured_output(ChartGenerationResponse) → Pydantic 对象   parse_method = structured
2) json.loads(raw) → 拿 (option, content)                               parse_method = primary
3) 扫围栏 → 围栏里是 {option, content}                                  parse_method = fence_full
4) 围栏里只是裸 ECharts option                                          parse_method = fence_option
5) 裸对象 + 顶层有 content 字段 → 抽出 content                           parse_method = in_option
6) 围栏 + content-in-option 变种                                       parse_method = fence_in_option
7) 还不行 → 502 + raw_reply
```

`structured` 和 `primary` 是最理想的 parse_method。前端非两者时顶部显示「⚠ LLM 偏离 schema」红色徽章。

### Provider 推理深度适配

`_detect_provider` 从 Base URL 嗅探 provider（`:11434`/`/ollama` → ollama；`bigmodel.cn`/`zhipu` → glm；其它 → openai），自动调整 thinking 字段语义：

| Provider | off | low/medium/high |
|---|---|---|
| openai（含 DeepSeek/DashScope） | 不发 | `reasoning_effort` |
| ollama | `reasoning_effort: "none"` | `reasoning_effort` |
| glm（智谱 GLM-4.5+） | `thinking: {type:"disabled"}` | `thinking: {type:"enabled"}` |

### LangChain 模块

| 模块 | 路径 | 用途 |
|---|---|---|
| Prompt 注册中心 | `prompts/__init__.py` | 统一从一个模块导入所有 prompt |
| Chain 构建器 | `chains/base.py` | `StructuredLLMChain` — 声明式 Chain 装配 |
| Pydantic 数据模型 | `output_parsers/schema.py` | ColumnSchema / DataUnderstandingResponse / ChartGenerationResponse / ProviderConfig / PipelineEvent |
| 数据理解链 | `chains/understanding.py` | 输出绑定 DataUnderstandingResponse |
| 图表生成链 | `chains/chart_generation.py` | 输出绑定 ChartGenerationResponse，失败时自动降级 |
| 流水线 | `chains/pipeline.py` | event-based 6 阶段生成器 |
| Tools | `tools/*.py` | 知识库检索 / 数据预处理 / 图表类型选择 Tool |
| Agent | `agents/dataviz_agent.py` | DataViz Agent |
| Memory | `memory/chat_memory.py` | LangChainChatMessageHistory + ChatMemory |

`app.py` 的对外函数签名不变，`/api/parse` / `/api/chart` / `/api/chart/stream` 无需调整。

## API 参考

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 可视化对话页 |
| GET | `/config` | 同上（URL 形式，自动打开配置弹窗） |
| GET | `/api/config` | 读配置；Key 只返掩码 `sk-***abcd` + `llm_api_key_present` 布尔 |
| POST | `/api/config` | 存配置；Key 留空/null/缺失时**不覆盖**已存值 |
| POST | `/api/config/test` | 测试 LLM 连接；body 可用当前表单值覆盖 DB，无需先保存 |
| POST | `/api/parse` | 解析文件或文本；form 字段：`file` 或 `text`；可选 `use_llm=1` / `no_header=1` / `selected_sheets=` / `hint=`。返回 `{columns, rows, count, description, source, understand_method, summary, notes}` 或 `{needs_sheet_selection, sheets}` |
| POST | `/api/chart` | 非流式主生成接口。body `{prompt, data?, chart_type_hint?, style_hint?}`。返回 `{chart_type, option, code, content, explanation, raw_reply, parse_method, understanding?, preprocess?}` |
| POST | `/api/chart/stream` | 流式主生成接口（SSE）。事件类型：`stage`（阶段状态）/ `delta`（token 增量）/ `done`（最终结果）/ `error`（失败） |
| GET | `/api/knowledge?q=...` 或 `?chart_type=...` | 查本地知识库 |
| GET | `/api/chats` | 对话列表（当前默认只有一条 `default` 对话） |
| GET | `/api/chats/<id>/messages` | 指定对话下的消息列表 |

## 安全与隐私

- API Key 仅保存在本机 `app.db`，不写日志、不上传、不进代码仓库
- 所有 LLM 请求只发往你在配置页填写的 Base URL；可指向内网 Ollama / LM Studio / vLLM
- `.gitignore` 已忽略 `app.db`、`.env`、`__pycache__/`

## 部署建议

```bash
# 开发环境
python app.py

# 生产环境（gunicorn）
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8080 app:app

# 生产环境（waitress，Windows/Linux 通用）
pip install waitress
waitress-serve --host 0.0.0.0 --port 8080 app:app
```

## 依赖清单

```
flask>=3.0
flask-cors>=4.0
pandas>=2.0
openpyxl>=3.1
python-dateutil>=2.8
langchain>=0.1.0
langchain-core>=0.1.0
langchain-openai>=0.0.5
langchain-community>=0.0.10
pydantic>=2.0
```

## License

见仓库附带的 `LICENSE` 文件。
