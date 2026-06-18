# ECharts Agent · 可视化对话

一个基于 Flask + 任意 OpenAI 兼容 LLM 的 ECharts 图表生成助手。上传数据（Excel / CSV / JSON / 文本），用自然语言描述需求，即可生成交互式图表，并导出自包含的独立 HTML。

## 核心能力

- **自然语言生成图表**：用中文描述需求，模型自动选择图表类型、生成 ECharts 配置、完成渲染
- **多格式数据支持**：Excel（xlsx/xls，支持多 sheet）、CSV、JSON、纯文本（TSV，空格分隔等）
- **LLM 智能整理数据**：对不规范数据（多级表头、带单位、千分位、合并单元格、合计行等）自动清洗与列类型推断
- **本地规则预处理**：prompt 中的自然语言指令（"保留 2 位小数"、"前十"、"按月份分组求和"等）由本地规则引擎在 50ms 内完成，不消耗 LLM token
- **结构化输出 + 双模式**：主生成走 LangChain `with_structured_output(ChartGenerationResponse)`，LLM 可选 **option 模式**（严格 JSON）或 **code 模式**（完整 JS 代码，支持任意 ECharts 回调函数如 formatter）；流式 schema 中两个字段都 optional，模型按需选用；偏离时自动 5 层降级（围栏 → 裸 ECharts option → content-in-option），确保鲁棒
- **3 层防御 `{{ }}` 转义**：LLM 受 f-string / 模板语言训练数据副作用，偶尔会把 JSON 的 `{` 转义成 `{{` —— ① **prompt 源头已用单 `{ }`**（不再用 `{{ }}` 演示），② 服务端 schema 严格模式，③ parser 层 `_unescape_braces` + `_try_parse_json` 兜底（兼容所有边缘 case）
- **Code 模式 + iframe 沙箱**：当 LLM 用 `code` 字段输出 JS 代码时，前端把它发到 `static/sandbox.html`（`sandbox="allow-scripts"` 的 null-origin iframe）里执行，捕获 `chart.setOption(option)` 的 option 回主页面渲染；函数在 sandbox 内被 `toString` 成 `__fn__` 标记对象（跨 iframe postMessage 无法传函数），主页面 `new Function` 还原为真函数供 ECharts 调用；所有还原后的函数再被 `try/catch` 安全网包裹——单个 formatter 报错不会毁整个图表
- **推理深度控制**：支持 reasoning_effort（DeepSeek/OpenAI/o1）、thinking（GLM-4.5+）、reasoning（Ollama）字段，按 provider 自动适配
- **流式生成（SSE）**：逐 token 推送给前端，图表下方有 6 阶段进度面板（准备 → 整理 → 预处理 → 选类型 → 生成 → 解析）
- **ECharts 知识库**：内置 15 种图表类型的配置指导，按类型裁剪后注入 prompt，节省 15-25% token
- **多轮对话记忆**：通过 SQLite 持久化保存会话与消息
- **零隐私上传**：API Key 仅存本地 `app.db`；所有请求只发往你在配置页填写的 Base URL
- **内网可部署**：所有 JS 资源本地化，无 CDN 依赖

## 项目结构

```
echarts-agent/
├── app.py                      # Flask 入口：路由 / 配置持久化 / pipeline / SSE
├── llm_client.py               # LLM 调用封装：LangChain ChatOpenAI + Provider 策略
├── data_parser.py              # Excel / CSV / JSON / 文本统一解析
├── data_understanding.py       # LLM 数据理解与整理
├── data_preprocessing.py       # 本地规则预处理引擎
├── knowledge.py                # ECharts 知识库（15 种图表配置指导）
├── output_parsers/
│   └── schema.py               # Pydantic 数据模型：ProviderConfig / PipelineEvent 等
├── prompts/
│   └── __init__.py             # ChatPromptTemplate 中心
├── requirements.txt
├── scripts/
│   └── download_vendor.py      # 首次部署：拉取 echarts + dark 主题
├── templates/
│   └── chat.html
└── static/
    ├── app.css / app.js        # 流式 SSE + ECharts 渲染 + 配置弹窗 + sandbox 调度
    ├── sandbox.html            # null-origin iframe，执行 LLM 生成的 JS 代码
    └── vendor/echarts/
```

> 运行时在根目录生成 `app.db`（SQLite），已在 `.gitignore` 中忽略。

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

### 打包为 Windows 单文件 .exe

仓库自带 `echarts-agent.spec`，可在 Windows 上用 PyInstaller 一键打包（仅 Windows 环境）：

```powershell
pip install pyinstaller
pyinstaller echarts-agent.spec --noconfirm --clean
```

产物：`dist\echarts-agent.exe`（约 44 MB，单文件，含 ECharts 内嵌）。

**运行特性：**
- 启动后自动打开默认浏览器到 `http://127.0.0.1:8080/`
- 关闭浏览器窗口无法停服务；按 **Ctrl + C** 结束
- 运行时数据（`app.db`、上传的 Excel/CSV、生成的临时文件）写到 `%LOCALAPPDATA%\EChartsAgent\`，**不会污染 exe 所在目录**
- 模板与静态资源从 PyInstaller 的解压目录加载，对源代码 0 侵入
- 环境变量：`PORT=9000`、`HOST=0.0.0.0`、`OPEN_BROWSER=0`（关闭自动开浏览器）

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
  ├─ 5) 主生成                流式调用 → 解析 → 自动修正
  └─ 6) 解析与校验            Pydantic 优先 + 手写 5 层兜底
                               ↓
                          前端 echarts.setOption(option)
```

- **阶段 2 触发条件**：数据已有 `understand_method=llm`（解析时已整理）→ 直接复用；本次请求显式要求 → 调 LLM；都没有 → 跳过
- **阶段 3**：从 prompt 识别支持的规则（round / drop_null / group_sum / top_n / sort 等），无规则则跳过；规则信息直接挂到 `data.preprocess`，由 `build_chart_user_prompt` 内自动添加精度提示给 LLM（不再在 app.py 重复拼接）
- **阶段 4**：用户在下拉框选了类型则跳过 LLM 推荐
- **阶段 5**：非流式优先走 LangChain `with_structured_output(ChartGenerationResponse)` —— 自动注入 schema + 重试；流式用 `response_format=json_schema`，**支持 option / code 双模式**（schema 中两个字段都是 optional，模型按需选用）

### 结构化输出：Pydantic + 5 层手写兜底

主生成的解析路径（`app._parse_chart_response`）：

```
1) Pydantic ChartGenerationResponse.model_validate_json     parse_method = primary（双模式都走这里）
   - 先用 raw 校验一次；失败再用 _unescape_braces 后的版本重试（兼容 {{ }} 转义）
2) 手写兜底（仅识别 option，不识别 code）：
   2a) json.loads → {option, content}                       parse_method = primary
   2b) 顶层是裸 ECharts option，干净                         parse_method = primary_bare
   2c) 顶层是裸 ECharts option + 顶层有 content              parse_method = in_option
   2d) 扫 ```json``` 围栏 → 围栏里 {option, content}         parse_method = fence_full
   2e) 围栏里裸 ECharts option + 围栏外文字当 content        parse_method = fence_option
   2f) 围栏 + content-in-option 变种                         parse_method = fence_in_option
3) 还不行 → 502 + raw_reply + 「输出被截断」提示（首末字符判断）
```

非 `primary` / `primary_bare` 时前端顶部显示「⚠ LLM 偏离 schema」红色徽章；`is_code_mode=True` 时另显示「⚡ Code 模式」。

### Provider 推理深度适配

`_sniff_provider` 从 Base URL 嗅探 provider（`:11434`/`/ollama` → ollama；`bigmodel.cn`/`zhipu` → glm；其它 → openai），自动调整 thinking 字段语义：

| Provider | off | low/medium/high |
|---|---|---|
| openai（含 DeepSeek/DashScope） | 不发 | `reasoning_effort` |
| ollama | `reasoning_effort: "none"` | `reasoning_effort` |
| glm（智谱 GLM-4.5+） | `thinking: {type:"disabled"}` | `thinking: {type:"enabled"}` |

策略在 `output_parsers/schema.py::ProviderConfig.for_provider` 注册，新增 provider 只需扩一张表。

## 模块说明

| 模块 | 路径 | 作用 |
|---|---|---|
| HTTP 入口 | `app.py` | Flask 路由、SQLite 配置持久化、6 阶段 pipeline、SSE 推流 |
| LLM 封装 | `llm_client.py` | `ChatOpenAIWrapper`（按 `(stream, overrides)` 缓存 ChatOpenAI 实例）、`get_llm_wrapper`、`call_llm` / `call_llm_raw` / `call_llm_stream`、`pick_chart_type`、`build_chart_prompt`、`compute_column_stats` |
| Provider 策略 | `output_parsers/schema.py` | `ProviderConfig`：根据 Base URL 返回 thinking 字段映射；`ChartGenerationResponse` 双模式校验 |
| 数据解析 | `data_parser.py` | Excel 多 sheet 选择 + CSV / JSON / 文本统一入口 |
| 数据整理 | `data_understanding.py` | LLM 整理不规范数据；fallback 到代码解析 |
| 数据预处理 | `data_preprocessing.py` | 10 种本地规则（round / drop_null / dedup / iqr_outlier / group_sum / group_mean / sort / top_n / strip_thousands） |
| 知识库 | `knowledge.py` | 15 种图表的 ECharts 配置指导；按图表类型裁剪减少 token |
| Prompt 模板 | `prompts/__init__.py` | `DEFAULT_CHART_SYSTEM_PROMPT` + 三个 `ChatPromptTemplate`（chart 生成 / 类型推荐 / 数据整理）|
| Pipeline 协议 | `output_parsers/schema.py::PipelineEvent` | 事件 dict 的参考 schema |

`app.py` 的对外 API（`/api/parse` / `/api/chart` / `/api/chart/stream`）保持不变。

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
- `.gitignore` 已忽略 `app.db`、`.env`、`__pycache__/`、`data/`

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
waitress>=2.1
langchain>=0.1.0
langchain-core>=0.1.0
langchain-openai>=0.0.5
langchain-community>=0.0.10
pydantic>=2.0
```

## License

见仓库附带的 `LICENSE` 文件。