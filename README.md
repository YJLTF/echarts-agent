# ECharts Agent · 可视化图表生成 Agent

基于 **Apache ECharts** 的图表生成 Agent。只需提供数据 +自然语言需求，系统会自动选择合适的图表类型、生成可运行的 ECharts `option` JSON，并在页面上直接渲染。

## ✨ 主要功能

- 📝 **数据自动解析**：支持 `Excel (.xlsx / .xls)`、`CSV`、`JSON`、纯文本表格
  - **多 sheet xlsx**：上传后自动列出所有 sheet，每行展示「行数 × 列数 + 列名预览」+ 全选/全不选 + 确认合并（合并后自动加 `__sheet__` 列）
  - **首行是数据**开关：列名变 `字段1 / 字段2 / ...`
  - **智能日期识别**：用 `dateutil` 试解析，自动支持 `2024-01-15` / `2024/01/15` / `2024.01.15` / `2024年1月15日` / `1月15日` / `08:30:00` / `今天` / `Jan 2024` 等格式
- 🧠 **大模型智能整理（可选）**：对不规范数据可让大模型二次理解 ——
  剔除标题行 / 合计行 / 重复表头；去单位 / 千分位 / 百分号；
  规范化列名并推断 `string/number/date/boolean` 类型与 `category/value/time/series/label/ignore` 角色
- 🤖 **LLM 生成图表代码**：调用任意 OpenAI 兼容接口，返回标准 ECharts `option` 与可运行代码
- 🧠 **智能图表类型推荐**：自动判断更适合用柱状图/折线图/饼图/散点图…
- ⚙️ **配置弹窗**（不离开当前页）：点击主区右上「⚙️ 配置」即开，Esc / 背景 / ✕ 关闭；URL `pushState` 到 `/config` 保持可分享
- ⏱ **生成进度 + 取消**：图表下方蓝色横条 + 脉冲点 + 「✕ 取消」按钮，长任务可中断
- 📋 **图表结果多视图**：文字解释 / option JSON / JS 代码 / 原始回复；首轮 JSON 解析失败会自动让 LLM 重写一次，成功时响应带 `retried: true` + 前端「🔁 已自动修正」徽章
- 📥 **导出独立 HTML**：完全自包含（自带 ECharts 主库 + dark 主题），可双击离线打开
- 🎨 **可视化配置项知识库**：ECharts 配置项指导完全离线内置，**不会进行联网搜索**
- 🔒 **零隐私上传**：生成图表过程只调用你自己的模型服务；API Key 仅保存在本地 `app.db`
- 🌐 **内网可部署**：所有 JS 资源本地化在 `static/vendor/`，无任何 CDN 依赖（除用户配置的大模型 API）

## 🏗 项目结构

```
echarts-agent/
├── app.py                # Flask 入口：路由 / 配置持久化 / option 解析
├── llm_client.py         # OpenAI 兼容协议调用 + 图表类型推荐 + prompt 构造
├── data_parser.py        # Excel / CSV / JSON / 文本表格统一解析（代码层）
├── data_understanding.py # 大模型数据整理：理解不规范数据并输出规范 schema + rows
├── knowledge.py          # 本地配置项知识库（bar/line/pie/...15 种图表）
├── scripts/
│   └── download_vendor.py # 把 ECharts 等下载到 static/vendor/（首装 / 升级用）
├── templates/
│   └── chat.html         # 可视化对话页（含「智能整理」开关 + 配置弹窗 + 多 sheet 选择器）
└── static/
    ├── app.css           # 全站样式
    ├── app.js            # 对话页 + 配置弹窗交互逻辑
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
3. **数据不规范？勾选侧边栏的「🧠 用大模型智能整理」**再点解析 ——
   会先由代码解析得到草稿，再让大模型二次理解：
   - 去掉标题行 / 合计行 / 重复表头
   - 去掉单位（万/亿/%/元/¥/千分位 等）
   - 规范化列名（去除空格、合并多级表头）
   - 推断每列的 `string / number / date / boolean` 与 `category / value / time / series / label / ignore` 角色
   - 整理后下方会显示「🧠 LLM 整理」标签、摘要与整理说明
4. 图表下方选项卡可查看：**文字解释 / option JSON / JS 代码 / 原始回复**
5. 右上角「📥 导出代码」下载一个**完全脱机的独立 HTML**（自带 ECharts + dark 主题，可双击离线打开）

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

### 3. 图表生成流水线（并行优化）

```
                  ┌─ 0) LLM 整理数据（可选） ─┐  并行
用户 prompt ──→  ├─ 1) LLM 选图表类型       ─┤
                  └────────────────────────┘
                  ↓
                  2) 查本地知识库（该类型的 ECharts 配置说明）
                  ↓
                  3) LLM 生成完整 option JSON + 文字解释
                  ↓
                  4) 前端 echarts.setOption(option) 渲染
```

- 0/1 两个 LLM 调用**并行**（线程池），省 30-60s
- 2 是本地查表，无网络
- 3 是单次 LLM 调用（payload 较大，包含 KB 说明 + 数据）
- urllib 单次超时 300s；Flask `threaded=True`，长请求不阻塞其他用户

### 4. Prompt 构造

每次调用模型时，后端会把以下信息合成为一个请求：

```
【用户需求】...
【数据】字段名 + 若干行（超过 100 行时仅发送前 100 行 + 总行数）
【图表类型】bar / line / pie / scatter / radar / gauge / funnel / heatmap / sunburst / treemap / sankey / candlestick / boxplot / effectScatter / pictorialBar
【ECharts 配置项指导】来自 knowledge.py 的本地配置项说明（通用 + tooltip + legend + 轴 + 具体图表类型）
【输出要求】只输出一段包裹在 ```json ... ``` 中的合法 ECharts option JSON，随后在 JSON 之后给出简短文字解释
```

### 5. JSON 提取与渲染

- 从模型返回的文本里，优先使用正则匹配 ````json ... ```` 代码块；匹配不到时取「第一个 `{` 到最后一个 `}`」作为候选 JSON；两者都失败则返回错误供用户重试。
- 前端拿到 option 后调用 `echarts.init(...).setOption(option)` 完成渲染。
- 前端主题（Light / Dark）会在 `init` 时作为 `theme` 参数传入。
- 切换主题时只 dispose+重建图表实例，**不重建 echarts listener**（resize handler 全程单例）

## 🔧 主要 API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/` | 可视化对话页 |
| GET | `/config` | 同上（URL 形式，自动打开配置弹窗） |
| GET | `/api/config` | 读配置；API Key 只返掩码（`sk-***abcd`）与 `llm_api_key_present` 布尔 |
| POST | `/api/config` | 存配置；`llm_api_key` 留空 / null / 缺失时**不覆盖**已存值 |
| POST | `/api/config/test` | 测试 LLM 连接；body 可传当前表单值覆盖 DB，无需先保存 |
| POST | `/api/parse` | 解析上传的文件或粘贴的数据；multipart form 字段：<br>• `file` 或 `text` 二选一<br>• `use_llm=1` 可选 · 启用 LLM 整理<br>• `no_header=1` 可选 · 首行是数据<br>• `selected_sheets=` 可选 · 复选框对应的 sheet 名（多 sheet 时）<br>• `hint=` 可选 · 用户意图，给 LLM 看的<br>返回 `{columns, rows, count, description, source, understand_method, summary, notes, code_parsed}` 或 `{needs_sheet_selection, sheets}` |
| POST | `/api/chart` | 主生成接口；JSON body `{ prompt, data?, chart_type_hint?, style_hint? }`；`data.need_understanding=true` 会在生成前先让 LLM 整理数据。返回 `{ chart_type, option, code, explanation, raw_reply, understanding? }` |
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

主要 helper（`app.py` / `data_parser.py` / `app.js`）：

| 位置 | 名字 | 用途 |
|---|---|---|
| `app.py` | `config_required` | 装饰器：缺 LLM 配置返 428 |
| `app.py` | `build_llm_cfg` | 一次性拼装 LLM 调用所需的所有 cfg 字段 |
| `app.py` | `parse_bool` | 兼容 form / query / json body 的布尔解析 |
| `app.py` | `extract_json` | 从 LLM 回复里抽 option / code / explanation；解析失败时先试 `_try_fix_json`（去尾随逗号 / 注释） |
| `app.py` | `_try_fix_json` | 修复 LLM 输出里最常见的 JSON 小毛病：行注释、块注释、尾随逗号 |
| `app.py` | `_auto_retry_parse_json` | 解析仍失败时，**自动让 LLM 重写一次**（温度 0.3）；成功就在响应里带 `retried: true`，前端显示「🔁 已自动修正」徽章 |
| `data_parser.py` | `_read_excel_to_data` | 读单个 sheet；自动做日期启发式推断 |
| `data_parser.py` | `_parse_json_object` | JSON → `{columns, rows, count}` 统一形态 |
| `data_parser.py` | `_parse_multiple_sheets` | 多 sheet 合并，加 `__sheet__` 列 |
| `data_parser.py` | `_looks_like_date_column` | 用 `dateutil.parser` 试解析前 8 个非空值；支持 ISO / 斜线 / 点 / 中文年月日 / 纯时间 / `今天/昨天/前天` / `Jan 2024`；纯数字不会被误认成日期 |
| `knowledge.py` | `get_knowledge_for_type` | **按图表类型裁剪 KB**：pie/funnel/sankey 不发 axis，gauge/heatmap/candlestick/boxplot 不发 legend，**单次 prompt 节省 15-25% token** |
| `knowledge.py` | `CHART_USES_AXIS` | 各图表是否需要 xAxis/yAxis 的事实表 |
| `llm_client.py` | `compute_column_stats` | 给一份行数据算每列紧凑统计：<20 行不计算；<100 行只给 min/max/mean/distinct；≥100 行额外加 median / p25 / p75 / IQR，让 LLM 既看均值也看分布中段，**不被 outlier 带偏** |
| `app.py` | `_DEFAULT_SYSTEM_PROMPT` | 默认 system prompt 含 6 条硬约束 + bar/line 最小可用结构示例；在 ⚙️ 配置里不填就用这个，填了则用用户的 |
| `static/app.js` | `ensureChart(theme)` | 同主题复用 / 异主题重建；不重复注册 resize listener |
| `static/app.js` | `setChartTitle(text, color)` | 占位 / 错误 / 取消的统一标题写法 |
| `static/app.js` | `buildParseFormData(opts)` | 解析提交的 FormData 构造一处搞定 |
| `static/app.js` | `resetDataUI()` | 解析前的 UI 重置一处搞定 |
| `static/app.js` | `submitParse(fd, btn)` | 提交流程 + 按钮 disabled 状态管理 |

## 📜 License

随仓库附带的 `LICENSE` 文件为准。
