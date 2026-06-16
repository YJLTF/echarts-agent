# ECharts Agent · 可视化图表生成 Agent

基于 **Apache ECharts** 的图表生成 Agent。只需提供数据 + 自然语言需求，系统会自动选择合适的图表类型、生成可运行的 ECharts `option` JSON，并在页面上直接渲染。

## ✨ 主要功能

- 📝 **数据自动解析**：支持 `Excel (.xlsx / .xls)`、`CSV`、`JSON`、纯文本表格
  - **多 sheet xlsx**：上传后自动列出所有 sheet，每行展示「行数 × 列数 + 列名预览」+ 全选/全不选 + 确认合并（合并后自动加 `__sheet__` 列）
  - **首行是数据**开关：列名变 `字段1 / 字段2 / ...`
  - **智能日期识别**：用 `dateutil` 试解析，自动支持 `2024-01-15` / `2024/01/15` / `2024.01.15` / `2024年1月15日` / `1月15日` / `08:30:00` / `今天` / `Jan 2024` 等格式
- 🧠 **LLM 智能整理数据（解析时）**：对不规范数据可让大模型二次理解 ——
  剔除标题行 / 合计行 / 重复表头；去单位 / 千分位 / 百分号；
  规范化列名并推断 `string/number/date/boolean` 类型与 `category/value/time/series/label/ignore` 角色
  整理结果会带 `understand_method=llm/fallback` 字段，**生成图表时被自动复用**，不会再调一次 LLM
- 🔁 **数据预处理（生成时，本地规则）**：从用户需求里识别数据处理指令并直接改写数据，< 50ms 完成
  - `保留 N 位小数` / `round to N decimals` / `取整`
  - `<列名> 保留 N 位小数` / `把 销售额 保留 1 位`（单列）
  - `去除空值` / `drop nulls` / `去重` / `dedup`
  - `去除异常值` / `drop outliers` —— 1.5×IQR 整行删除
  - `按 X 分组求和/求平均` / `group by X sum/mean`
  - `按 X 升序/降序` / `sort by X asc/desc`
  - `前 N 大/小` / `按 X 取前 5 小` / `top 5`
  - `去掉千分位` / `strip thousands`
  - **支持中文数字写法**：`保留两位小数` / `前十` / `前十二大` 都能识别（`一月份` 这类列名不会被误改）
  - 预处理摘要会**附加到图表生成 prompt**，让 LLM 知道数据已经被改，并在 `tooltip/axisLabel` 里用一致精度
- 🤖 **LLM 生成图表代码**：调用任意 OpenAI 兼容接口，返回标准 ECharts `option` 与可运行代码
- 🧠 **智能图表类型推荐**：自动判断更适合用柱状图/折线图/饼图/散点图…
- ⚙️ **配置弹窗**（不离开当前页）：点击主区右上「⚙️ 配置」即开，Esc / 背景 / ✕ 关闭；URL `pushState` 到 `/config` 保持可分享
- 📊 **生成进度面板**（流式）：图表下方折叠面板，按 6 阶段（数据准备 → 智能整理 → 数据预处理 → 选类型 → 生成 → 解析）逐个高亮、✓ / ✕ / — 状态；生成阶段实时滚动模型 token；流式协议 `text/event-stream`，避免 nginx 代理缓冲
  - 长任务期间可随时点「✕ 取消」（`AbortController`）
  - 完成后显示耗时（`耗时 12s`）
- 📋 **图表结果多视图**：文字解释 / option JSON / JS 代码 / 原始回复；首轮 JSON 解析失败会自动让 LLM 重写一次，成功时响应带 `retried: true` + 前端「🔁 已自动修正」徽章
- 📥 **导出独立 HTML**：完全自包含（自带 ECharts 主库 + dark 主题），可双击离线打开
- 🎨 **可视化配置项知识库**：ECharts 配置项指导完全离线内置，**不会进行联网搜索**
- ⌨️ **快捷键**：`Ctrl/⌘+Enter` 在 prompt / 数据输入框直接生成图表
- 🔒 **零隐私上传**：生成图表过程只调用你自己的模型服务；API Key 仅保存在本地 `app.db`
- 🌐 **内网可部署**：所有 JS 资源本地化在 `static/vendor/`，无任何 CDN 依赖（除用户配置的大模型 API）

## 🏗 项目结构

```
echarts-agent/
├── app.py                # Flask 入口：路由 / 配置持久化 / chart 流水线 / SSE 流式输出
├── llm_client.py         # OpenAI 兼容协议调用（非流式 + 流式）+ 图表类型推荐 + prompt 构造
├── data_parser.py        # Excel / CSV / JSON / 文本表格统一解析（代码层）
├── data_understanding.py # 大模型数据整理：理解不规范数据并输出规范 schema + rows
├── data_preprocessing.py # 规则引擎：识别「保留 N 位小数 / 前 N 大 / 去除空值…」并本地改写
├── knowledge.py          # 本地配置项知识库（bar/line/pie/...15 种图表）
├── requirements.txt      # Python 依赖清单
├── scripts/
│   └── download_vendor.py  # 把 ECharts 等下载到 static/vendor/（首装 / 升级用）
├── templates/
│   └── chat.html         # 可视化对话页（含解析阶段 🧠 + 生成阶段 🧠 + 6 阶段进度面板 + 配置弹窗 + 多 sheet 选择器）
└── static/
    ├── app.css           # 全站样式（含 prefers-reduced-motion / 暗色滚动条）
    ├── app.js            # 对话页 + 配置弹窗 + SSE 流式消费 + 进度面板渲染
    └── vendor/echarts/   # 本地 ECharts（首装时由 scripts/download_vendor.py 拉取）
        ├── echarts.min.js
        └── dark.js
```

运行时会在项目根目录生成 `app.db`（SQLite，保存配置与对话历史），已在 `.gitignore` 中忽略。

## 🚀 快速开始

### 环境要求

- Python ≥ 3.9
- 浏览器（Chrome / Edge / Firefox 较新版本即可渲染 ECharts）

### 安装依赖

```bash
cd /path/to/echarts-agent
pip install -r requirements.txt
```

### 启动服务

```bash
python3 app.py
# 默认端口 8080，可用 PORT 环境变量覆盖：
PORT=8000 python3 app.py
```

### 浏览器中使用

1. **先配置**：打开 http://127.0.0.1:8080/ ，点主区右上「⚙️ 配置」
   - Base URL：例如 `https://api.openai.com/v1` / `https://api.deepseek.com/v1` / `https://dashscope.aliyuncs.com/compatible-mode/v1` / `http://localhost:11434/v1`（Ollama）
   - API Key：对应平台的 Key（已配置的会以 `sk-***abcd` 掩码显示，「修改」按钮才会露出输入框）
   - 模型名：例如 `gpt-4o-mini` / `deepseek-chat` / `qwen-plus` / `glm-4.5-air` 等
   - 可选：自定义 System Prompt、Temperature、Max Tokens
   - 「🔌 测试连接」会**用表单当前值**直接调一次 LLM（不需要先保存）→ 确认后再「💾 保存」
2. **再对话**：http://127.0.0.1:8080/
   - 上传文件（.xlsx/.xls/.csv/.json/.txt）或粘贴 CSV/JSON 数据
   - **多 sheet xlsx**：先弹 sheet 选择器，勾选后确认才解析
   - 输入需求，例如：
     > 用数据里的「月份」作为 X 轴，「销售额」作为 Y 轴，画一个柱状图并带圆角。
   - 点「✨ 生成图表」，几十秒到一两分钟后即可看到渲染结果
   - 长任务期间图表下方有进度条 + 「✕ 取消」按钮
    3. **数据不规范？勾选侧边栏的「🧠 LLM 智能整理数据」**再点解析 ——
    会先由代码解析得到草稿，再让大模型二次理解：
    - 去掉标题行 / 合计行 / 重复表头
    - 去掉单位（万/亿/%/元/¥/千分位 等）
    - 规范化列名（去除空格、合并多级表头）
    - 推断每列的 `string / number / date / boolean` 与 `category / value / time / series / label / ignore` 角色
    - 整理后下方会显示「🧠 LLM 整理」标签、摘要与整理说明
    4. **在需求里写数据处理指令**，例如「保留 2 位小数」「前 10 大」「按月份分组求和」「去除空值」——
    本地规则引擎会自动应用到数据，并显示在生成进度面板的「数据预处理」阶段。
    解析时没勾 🧠 但又想 LLM 整理？发送前勾「3. 发送需求」下的「🧠 用 LLM 智能整理数据（生成时）」即可补救。
  5. 图表下方选项卡可查看：**文字解释 / option JSON / JS 代码 / 原始回复**
  6. 右上角「📥 导出代码」下载一个**完全脱机的独立 HTML**（自带 ECharts + dark 主题，可双击离线打开）
  7. 常用快捷键：`Ctrl/⌘ + Enter` 在 prompt / 数据输入框直接生成图表

## 🧩 核心设计

### 1. 页面布局

```
┌─────────────┬───────────────────────────────────────────┐
│  brand      │  对话                          ⚙️ 配置 │
│             │                            📥 导出  ⛶ 全屏│
│ 1. 数据     │  ┌────────────────────────────────────┐  │
│   上传/粘贴 │  │            chart 渲染区             │  │
│   复选框    │  └────────────────────────────────────┘  │
│   解析/清空 │  进度条 + 取消（生成时显示）             │
│   sheet选择 │                                            │
│   数据预览  │  推荐类型 + 整理摘要                      │
│             │  ┌── tabs ────────────────────────────┐  │
│ 2. 样式     │  │ 文字解释 / option / JS / 原始回复   │  │
│   主题/类型 │  └────────────────────────────────────┘  │
│   标题      │                                            │
│ ─────────  │                                            │
│ 3. 发送需求 │                                            │
│   prompt    │                                            │
│   生成/示例 │                                            │
│   状态提示  │                                            │
└─────────────┴───────────────────────────────────────────┘
```

- 侧栏底部的「3. 发送需求」**固定贴底**，始终可见，不需滚动
- 「⚙️ 配置」在主区右上，**弹窗形式**（不离开对话页，状态/数据不丢）
- URL `pushState` 维护 `/config` 可分享可收藏

### 2. 数据解析：代码 + 大模型双层 + 多 sheet

第一层是 `data_parser.py` 的代码解析（pandas / csv / json / 空格嗅探），能搞定 90% 的「标准」数据。

**多 sheet xlsx 流程**：
```
上传 xlsx
  → 后端 list_excel_sheets(raw) 列出所有 sheet
  → 1 个 sheet：直接解析返回
  → ≥2 个 sheet：返回 {needs_sheet_selection: true, sheets: [...]}
       → 前端弹选择器，用户勾选后回传 selected_sheets=[...]
       → 后端按勾选顺序 pd.concat 合并，加 __sheet__ 列
```

**对于不规范数据**，可启用第二层 —— `data_understanding.py`：
- 输入：原始文本片段 + 代码解析草稿（列名 + 前几行）
- LLM 输出严格 JSON：`{ columns: [{name, type, role, description}], rows, summary, notes }`
- 后端会做：JSON 抽取与 schema 校验、单位 / 千分位 / 百分号自动转 number、未知列填默认类型
- LLM 调用失败 / JSON 解析失败时**自动回退**到代码解析结果，不会阻塞前端

调用方式：
- `POST /api/parse`，form 字段加 `use_llm=1`（可选 `hint=...`）
- 或 `POST /api/chart` 时 `data.need_understanding=true`

### 3. 图表生成流水线（6 阶段 + 流式）

按 6 阶段顺序执行，每阶段都会向前端推 `stage` 事件；主生成阶段还会逐 chunk 推 `delta` 事件（OpenAI 兼容 `text/event-stream`）。前端把这些事件渲染成「生成进度面板」+ 模型实时输出区。

```
用户 prompt + data ─→ ┌─ 1) 数据准备            瞬时
                       ├─ 2) 智能数据整理        复用 / 调一次 LLM / 跳过
                       ├─ 3) 数据预处理（本地）  < 50ms 规则改写数据
                       ├─ 4) 选择图表类型        调一次 LLM（可被 typeSel 覆盖）
                       └─ 5) 主生成              流式调用 / 解析 / 自动修正重试
                                                ↓
                                          前端 echarts.setOption(option) 渲染
```

- **阶段 2 触发条件**（按优先级）：
  - 数据已有 `understand_method ∈ {llm, fallback}`（即解析时已整理）→ **直接复用**，不重跑
  - 提交时 `data.need_understanding=true`（生成阶段勾了 🧠）→ 调一次
  - 都没有 → `skipped`
- **阶段 3 触发条件**：从 prompt 里识别到任何支持的规则就执行，否则 `skipped`
- **阶段 4**：用户在「图表类型」下拉里手动选了值就跳过 LLM 推荐
- **阶段 5**：调用 LLM 的主生成；流式返回时把每个 token 推给前端。若首轮 JSON 解析失败，自动以 0.3 温度重试一次，成功时响应里带 `retried: true`，前端显示「🔁 已自动修正」徽章
- 主生成 LLM 调用 urlopen 超时 300s；Flask `threaded=True`，长请求不阻塞其他用户
- SSE 响应带 `X-Accel-Buffering: no` 头，防止 nginx 等反向代理把流式响应攒成大块

### 4. Prompt 构造

每次调用模型时，后端会把以下信息合成为一个请求：

```
【用户需求】...
【数据】字段名 + 若干行（超过 100 行时仅发送前 100 行 + 总行数）
【图表类型】bar / line / pie / scatter / radar / gauge / funnel / heatmap / sunburst / treemap / sankey / candlestick / boxplot / effectScatter / pictorialBar
【数据预处理已应用】（如有）—— 已生效的规则列表，让 LLM 知道数据被改过，要在 tooltip/axisLabel 里用一致精度
【ECharts 配置项指导】来自 knowledge.py 的本地配置项说明（通用 + tooltip + legend + 轴 + 具体图表类型）
【输出要求】只输出一段包裹在 ```json ... ``` 中的合法 ECharts option JSON，随后在 JSON 之后给出简短文字解释
```

### 5. JSON 提取与渲染

- 从模型返回的文本里，优先使用正则匹配 ````json ... ```` 代码块；匹配不到时取「第一个 `{` 到最后一个 `}`」作为候选 JSON；两者都失败则自动重试一次。
- 前端拿到 option 后调用 `echarts.init(...).setOption(option)` 完成渲染。
- 前端主题（Light / Dark）会在 `init` 时作为 `theme` 参数传入。
- 切换主题时只 dispose+重建图表实例，**不重建 echarts listener**（resize handler 全程单例）

### 6. 数据预处理规则（生成时本地执行）

`data_preprocessing.py` 是一个纯本地、< 50ms 完成的规则引擎，从用户 prompt 里识别数据处理指令并直接改写 `data`，再交给图表生成 LLM：

| 用户 prompt 示例 | 触发的规则 |
| --- | --- |
| `保留 2 位小数` / `round to 2 decimals` / `取整` | `round`（全表） |
| `销售额 保留 1 位小数` / `把 销售额 保留 1 位` / `"value" 保留 1 位` | `round_col`（单列） |
| `保留两位小数` / `前十` / `前十二大` | 中文数字归一化器先把「两/十/十二」转成阿拉伯再匹配 |
| `去除空值` / `drop nulls` / `去掉空行` | `drop_null` |
| `去重` / `dedup` | `dedup` |
| `去除异常值` / `drop outliers` | `iqr_outlier`（1.5×IQR 整行删除） |
| `按月份分组求和` / `group by month sum` | `group_sum` |
| `按月份分组求平均` / `group by month mean` | `group_mean` |
| `按销售额降序` / `sort by sales desc` | `sort` |
| `前 10 大` / `前十大` / `按销售额取前 5` / `top 5` | `top_n` |
| `去掉千分位` / `strip thousands` | `strip_thousands` |

识别失败的规则会**安全跳过**而不阻塞后续；所有规则按固定优先级生效（与 prompt 中出现顺序无关）；列名解析做了防误伤处理（`请保留` 不会把「请」当列名、`一月份` 不会把「一」归一化成 1）。

预处理动作会同时：
1. 改写 `data.rows` 真正去改数据
2. 摘要（`preprocess.summary`）回传到前端的「理由」区
3. 摘要附加到主生成的 prompt，让 LLM 在 `tooltip/axisLabel/series.label` 里用一致精度

## 🔧 主要 API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/` | 可视化对话页 |
| GET | `/config` | 同上（URL 形式，自动打开配置弹窗） |
| GET | `/api/config` | 读配置；API Key 只返掩码（`sk-***abcd`）与 `llm_api_key_present` 布尔 |
| POST | `/api/config` | 存配置；`llm_api_key` 留空 / null / 缺失时**不覆盖**已存值 |
| POST | `/api/config/test` | 测试 LLM 连接；body 可传当前表单值覆盖 DB，无需先保存 |
| POST | `/api/parse` | 解析上传的文件或粘贴的数据；multipart form 字段：<br>• `file` 或 `text` 二选一<br>• `use_llm=1` 可选 · 启用 LLM 整理<br>• `no_header=1` 可选 · 首行是数据<br>• `selected_sheets=` 可选 · 复选框对应的 sheet 名（多 sheet 时）<br>• `hint=` 可选 · 用户意图，给 LLM 看的<br>返回 `{columns, rows, count, description, source, understand_method, summary, notes, code_parsed}` 或 `{needs_sheet_selection, sheets}` |
| POST | `/api/chart` | 主生成接口（JSON 一次性返回，向后兼容）。body `{ prompt, data?, chart_type_hint?, style_hint? }`；`data.need_understanding=true` 会在生成前先让 LLM 整理数据。返回 `{ chart_type, option, code, explanation, raw_reply, understanding?, preprocess?, retried? }` |
| POST | `/api/chart/stream` | **主生成接口（SSE 流式）**。body 同上；响应 `text/event-stream`，事件类型：<br>• `data: {"type":"stage","stage":"<name>","status":"start|done|skipped|error", ...}` — 各阶段状态变更<br>• `data: {"type":"delta","content":"..."}` — 主生成阶段模型 token 增量<br>• `data: {"type":"done","chart_type":...,"option":...,"code":...,"explanation":...,"understanding":...,"preprocess":...,"retried":bool}`<br>• `data: {"type":"error","message":"...","raw_reply":"..."}` — 失败事件 |
| GET | `/api/knowledge?q=...` 或 `?chart_type=...` | 查本地知识库 |
| GET | `/api/chats` | 已有对话列表（当前默认只有一条 `default` 对话） |
| GET | `/api/chats/<id>/messages` | 指定对话下的消息列表 |

## 🔐 安全 & 隐私

- API Key 仅保存在本地的 `app.db`（SQLite），不会被写进代码、日志或上传到任何第三方服务。
- 所有模型请求仅发送到你填写的 Base URL；如果你想完全离线，也可以把 Base URL 指向本地运行的 Ollama / LM Studio / vLLM（只要它们对外暴露 OpenAI 兼容协议即可）。
- `.gitignore` 已忽略 `app.db`、`.env`、`__pycache__/` 等本地产物，提交代码时不会泄露。
- 弹窗内的「👁 显示」可在调试时临时查看 Key，但状态默认是密码型 + 掩码。

## 🌐 内网 / 脱机部署

本项目已**完全本地化**了所有非模型的网络依赖：

| 资源 | 来源 | 备注 |
|---|---|---|
| ECharts 主库 | `static/vendor/echarts/echarts.min.js` | 由 `scripts/download_vendor.py` 拉取（1 MB） |
| ECharts dark 主题 | `static/vendor/echarts/dark.js` | 同上（6 KB） |
| 应用 CSS / JS | `static/app.css`、`static/app.js` | 本仓库自带 |
| 大模型 API | 你在「⚙️ 配置」里填写的 Base URL | 这是唯一一处主动外联，可指向内网 LLM 服务 |

**首次部署 / 升级 vendor 时**（需要一次性的网络访问，用来拉 ECharts）：

```bash
python scripts/download_vendor.py                # 下载全部 vendor 资源
python scripts/download_vendor.py --list         # 只看要下载什么
python scripts/download_vendor.py --version 5.5.0  # 指定 ECharts 版本
python scripts/download_vendor.py --force        # 强制覆盖已有文件
```

下载后 `static/vendor/echarts/` 会被**提交进仓库**（不在 `.gitignore` 内），从此整个服务对外只访问大模型 API。

**导出的独立 HTML**：右上角「📥 导出代码」生成的 HTML 已经把 ECharts 主库 + dark 主题**内联进去**，可在任意完全离线的机器上双击打开，无需任何网络。

## 📦 部署（生产建议）

开发环境直接 `python3 app.py` 已经足够；生产环境建议：

```bash
# 使用 gunicorn + 多 worker
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8080 app:app

# 或者用 waitress（Windows / Linux 通用）
pip install waitress
waitress-serve --host 0.0.0.0 --port 8080 app:app
```

`app.run()` 已经启用 `threaded=True`，长 LLM 请求不阻塞其他用户。前面再挂一层 Nginx 做反向代理 + HTTPS 即可。

## 🛠 代码结构小记

主要 helper（`app.py` / `llm_client.py` / `data_parser.py` / `data_preprocessing.py` / `app.js`）：

| 位置 | 名字 | 用途 |
|---|---|---|
| `app.py` | `config_required` | 装饰器：缺 LLM 配置返 428 |
| `app.py` | `build_llm_cfg` | 一次性拼装 LLM 调用所需的所有 cfg 字段 |
| `app.py` | `parse_bool` | 兼容 form / query / json body 的布尔解析 |
| `app.py` | `_get_param` | 从 form / query / JSON body 中按优先级取参（Flask 缓存 `request.get_json()`，多次调用无开销） |
| `app.py` | `now_iso` | 时区感知的 UTC ISO8601 时间戳（避免 `datetime.utcnow()` 的 deprecation warning） |
| `app.py` | `run_chart_pipeline` | 6 阶段流水线生成器（prepare → understand → preprocess → pick_type → generate → parse）；`/api/chart` 消费 done 事件，`/api/chart/stream` 直接把事件转 SSE |
| `app.py` | `_retry_parse_json` | 主生成 JSON 解析失败时**自动让 LLM 重写一次**（温度 0.3），通过 `yield from` 把 delta 事件透传；返回 `(new_raw, parsed, error_event)` 三元组 |
| `app.py` | `extract_json` | 从 LLM 回复里抽 option / code / explanation；解析失败时先试 `_try_fix_json`（去尾随逗号 / 注释） |
| `app.py` | `_scan_json_object` | 栈式扫描 raw 中第一个完整顶层 JSON 对象（处理字符串字面量与 `}` 转义），替代 `rfind('}')` 兜底切片；候选超 500KB 直接放弃 |
| `app.py` | `_try_fix_json` | 修复 LLM 输出里最常见的 JSON 小毛病：行注释、块注释、尾随逗号 |
| `app.py` | `_strip_js_functions` | 状态机抠出 `function (…) {…}` 字面量换占位符（UUID）让 `json.loads` 通过；前端按 `fn_map` 还原成真函数 |
| `app.py` | `_build_retry_prompt` | 「上一轮回复无法解析」的修正 prompt 模板 |
| `app.py` | `_DEFAULT_SYSTEM_PROMPT` | 默认 system prompt 含 6 条硬约束 + bar/line 最小可用结构示例；在 ⚙️ 配置里不填就用这个，填了则用用户的 |
| `data_parser.py` | `_read_excel_to_data` | 读单个 sheet；自动做日期启发式推断 |
| `data_parser.py` | `_parse_json_object` | JSON → `{columns, rows, count}` 统一形态 |
| `data_parser.py` | `_parse_multiple_sheets` | 多 sheet 合并，加 `__sheet__` 列 |
| `data_parser.py` | `_decode_text` | bytes → str 多编码嗅探（utf-8 / utf-8-sig / gbk / latin-1），`data_understanding.py` 复用 |
| `data_parser.py` | `_looks_like_date_column` | 用 `dateutil.parser` 试解析前 8 个非空值；支持 ISO / 斜线 / 点 / 中文年月日 / 纯时间 / `今天/昨天/前天` / `Jan 2024`；纯数字不会被误认成日期 |
| `data_understanding.py` | `understand_data` | LLM 数据整理主入口；失败自动回退到代码解析结果 |
| `data_understanding.py` | `_extract_json` / `_validate` | 从 LLM 输出抽 JSON 并校验 schema（columns/rows/types/roles） |
| `data_understanding.py` | `_to_number` | 剥单位/千分位/百分号 → float；百分号自动 `/100` |
| `data_preprocessing.py` | `preprocess_data` | 规则引擎主入口：返回 `(new_data, info)`；info 包含每条规则的动作描述 |
| `data_preprocessing.py` | `_parse_rules` | 从 prompt 提取 10 类规则；先 `_normalize_cn_numbers` 把「保留两位小数 / 前十大 / 十二」等中文数字归一化 |
| `data_preprocessing.py` | `_normalize_cn_numbers` | 中文数字归一化器（0–99 范围），1 位数字要求后跟「量词/分隔/结尾」避免破坏列名 |
| `data_preprocessing.py` | `_try_number` | 字符串 → float 解析（剥单位 / 千分位 / 百分号，但**不**自动除 100）；与 `_to_number` 配合：前者用于「能否转数」判断，后者用于 LLM 输出清理 |
| `data_preprocessing.py` | `_group_aggregate` | `group_sum` / `group_mean` 共享的聚合代码 |
| `data_preprocessing.py` | `_apply_numeric_round_to_row` | `round` / `round_col` 共享的逐行 round 代码 |
| `data_preprocessing.py` | `_is_numeric_col` | 判断一列是不是数值列（70% 阈值，可被字符串数字触发） |
| `knowledge.py` | `get_knowledge_for_type` | **按图表类型裁剪 KB**：pie/funnel/sankey 不发 axis，gauge/heatmap/candlestick/boxplot 不发 legend，**单次 prompt 节省 15-25% token** |
| `knowledge.py` | `CHART_USES_AXIS` | 各图表是否需要 xAxis/yAxis 的事实表 |
| `llm_client.py` | `call_llm` / `call_llm_stream` | 非流式 / 流式调用；流式按 SSE 解析 `data: {json}\n\n`，遇 `application/json` 自动降级为一次性 yield |
| `llm_client.py` | `pick_chart_type` | 让 LLM 推荐图表类型；用户手动选了 `chart_type_hint` 时直接跳过 |
| `llm_client.py` | `compute_column_stats` | 给一份行数据算每列紧凑统计：<20 行不计算；<100 行只给 min/max/mean/distinct；≥100 行额外加 median / p25 / p75 / IQR，让 LLM 既看均值也看分布中段，**不被 outlier 带偏** |
| `llm_client.py` | `build_chart_prompt` | 把数据 / 需求 / 样式 / KB 拼成最终给 LLM 的 user prompt |
| `static/app.js` | `buildChartBody()` | `/api/chart` 与 `/api/chart/stream` 共用的请求体构造（含「🧠 生成时整理」勾选逻辑） |
| `static/app.js` | `consumeStream()` | 用 `ReadableStream.getReader()` 解析 SSE，按 `\n\n` 切事件，喂给 `handleStreamEvent` |
| `static/app.js` | `handleStreamEvent` | 单一入口处理所有 SSE 事件；`DETAIL_BY_STATUS` 表替代原本 60 行的 if/else |
| `static/app.js` | `setGenBusy(busy)` | 生成中禁用「生成图表」按钮 + 记录开始时间（用于显示耗时） |
| `static/app.js` | `hideChartStatus()` | 隐藏顶部状态条；替代直接 `setChartStatus("", false)` 的 8 处调用 |
| `static/app.js` | `ensureChart(theme)` | 同主题复用 / 异主题重建；不重复注册 resize listener |
| `static/app.js` | `setChartTitle(text, color)` | 占位 / 错误 / 取消的统一标题写法 |
| `static/app.js` | `buildParseFormData(opts)` | 解析提交的 FormData 构造一处搞定 |
| `static/app.js` | `resetDataUI()` | 解析前的 UI 重置一处搞定 |
| `static/app.js` | `submitParse(fd, btn)` | 提交流程 + 按钮 disabled 状态管理 |

## 📜 License

随仓库附带的 `LICENSE` 文件为准。
