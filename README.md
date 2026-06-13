# ECharts Agent · 可视化图表生成 Agent

基于 **Apache ECharts** 的图表生成 Agent。只需提供数据 + 自然语言需求，系统会自动选择合适的图表类型、生成可运行的 ECharts `option` JSON，并在页面上直接渲染。

## ✨ 主要功能

- 📝 **数据自动解析**：支持 `Excel (.xlsx / .xls)`、`CSV`、`JSON`、纯文本表格
- 🤖 **LLM 生成图表代码**：调用任意 OpenAI 兼容接口，返回标准 ECharts `option` 与可运行代码
- 🧠 **智能图表类型推荐**：自动判断更适合用柱状图/折线图/饼图/散点图…
- 🎨 **可视化配置项知识库**：ECharts 配置项指导完全离线内置，**不会进行联网搜索**
- ⚙️ **配置页**：可在前端填写 Base URL / API Key / 模型名 / System Prompt，所有配置保存在本地 SQLite
- 🌐 **交互页**：上传数据 → 描述需求 → 生成图表；可切换「文字解释 / option JSON / JS 代码 / 原始回复」视图；一键导出独立 HTML
- 🔒 **零隐私上传**：生成图表过程只调用你自己的模型服务；API Key 仅保存在本地 `app.db`

## 🏗 项目结构

```
echarts-agent/
├── app.py              # Flask 入口：路由 / 配置持久化 / option 解析
├── llm_client.py       # OpenAI 兼容协议调用 + 图表类型推荐 + prompt 构造
├── data_parser.py      # Excel / CSV / JSON / 文本表格统一解析
├── knowledge.py        # 本地配置项知识库（bar/line/pie/...15 种图表）
├── templates/
│   ├── chat.html       # 可视化对话页
│   └── config.html     # 配置页
└── static/
    ├── app.css         # 全站样式
    ├── app.js          # 对话页交互逻辑
    └── config.js       # 配置页交互逻辑
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
# 如果当前目录下还没有 requirements.txt，也可直接：
pip install flask flask-cors pandas openpyxl
```

> 项目已内置本地知识库，**首次启动不需要联网**；调用大模型的请求仅会打到你在「配置页」填写的 Base URL。

### 启动服务

```bash
python3 app.py
# 默认端口 8080，可用 PORT 环境变量覆盖：
PORT=8000 python3 app.py
```

启动后日志里会显示：

```
[ECharts Agent] starting on http://localhost:8080
 * Running on http://127.0.0.1:8080
 * Running on http://10.30.47.231:8080
```

### 浏览器中使用

1. **先配置**：打开 http://127.0.0.1:8080/config ，填入
   - Base URL：例如 `https://api.openai.com/v1` / `https://api.deepseek.com/v1` / `https://dashscope.aliyuncs.com/compatible-mode/v1`
   - API Key：对应平台的 Key
   - 模型名：例如 `gpt-4o-mini`、`deepseek-chat`、`qwen-plus` 等
   - 可选：自定义 System Prompt、Temperature、Max Tokens
   - 点击「测试连接」验证 → 再点「保存」
2. **再对话**：打开 http://127.0.0.1:8080/
   - 上传文件（.xlsx/.xls/.csv/.json）或粘贴 CSV/JSON 数据
   - 也可以直接点页面右下角「加载示例」一键填入示例数据
   - 输入需求，例如：
     > 用数据里的「月份」作为 X 轴，「销售额」作为 Y 轴，画一个柱状图并带圆角。
   - 点「生成图表」，几秒后即可看到渲染结果
3. 图表下方选项卡可查看：**文字解释 / option JSON / JS 代码 / 模型原始回复**
4. 右上角「📥 导出代码」会下载一个可独立运行的 HTML 文件（不需要 Python 环境）

## 🧩 核心设计

### 1. Prompt 构造

每次调用模型时，后端会把以下信息合成为一个请求：

```
【用户需求】...
【数据】字段名 + 若干行（超过 100 行时仅发送前 100 行 + 总行数）
【图表类型】bar / line / pie / scatter / radar / gauge / funnel / heatmap / sunburst / treemap / sankey / candlestick / boxplot / effectScatter / pictorialBar
【ECharts 配置项指导】来自 knowledge.py 的本地配置项说明（通用 + tooltip + legend + 轴 + 具体图表类型）
【输出要求】只输出一段包裹在 ```json ... ``` 中的合法 ECharts option JSON，随后在 JSON 之后给出简短文字解释
```

### 2. JSON 提取与渲染

- 从模型返回的文本里，优先使用正则匹配 ````json ... ```` 代码块；匹配不到时取「第一个 `{` 到最后一个 `}`」作为候选 JSON；两者都失败则返回错误供用户重试。
- 前端拿到 option 后调用 `echarts.init(...).setOption(option)` 完成渲染。
- 前端主题（Light / Dark）会在 `init` 时作为 `theme` 参数传入。

### 3. 图表类型推荐

当用户没显式指定图表类型时，系统会先走一次「轻量」LLM 调用：

- 输入：用户需求 + 列名 + 前几行数据
- 输出：一行英文类型（如 `line`）+ 中文理由（如「用于展示时间序列趋势」）
- 再把选中的类型送入正式的「生成 option」流程

## 🔧 主要 API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/` | 可视化对话页 |
| GET | `/config` | 配置页 |
| GET / POST | `/api/config` | 读取 / 保存 LLM 配置 |
| POST | `/api/config/test` | 测试当前配置能否调用模型 |
| POST | `/api/parse` | 解析上传的文件或贴入的数据，返回 `{columns, rows, count, description, source}` |
| POST | `/api/chart` | 主生成接口，入参 `{ prompt, data?, chart_type_hint?, style_hint? }`；返回 `{ chart_type, option, code, explanation, raw_reply }` |
| GET | `/api/knowledge` | 查阅本地知识库 `q=饼图` 或 `chart_type=pie` |
| GET | `/api/chats` | 已有对话列表（当前默认只有一条 main 对话） |
| GET | `/api/chats/<id>/messages` | 指定对话下的消息列表 |

## 🔐 安全 & 隐私

- API Key 仅保存在本地的 `app.db`（SQLite），不会被写进代码、日志或上传到任何第三方服务。
- 所有模型请求仅发送到你填写的 Base URL；如果你想完全离线，也可以把 Base URL 指向本地运行的 Ollama / LM Studio / vLLM（只要它们对外暴露 OpenAI 兼容协议即可）。
- `.gitignore` 已忽略 `app.db`、`.env`、`__pycache__/` 等本地产物，提交代码时不会泄露。

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

前面再挂一层 Nginx 做反向代理 + HTTPS 即可。

## 📜 License

随仓库附带的 `LICENSE` 文件为准。
