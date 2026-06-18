"""prompts —— Prompt 模板中心。

把 system prompt / user prompt 模板从业务代码里抽出来，用 LangChain 的
:class:`ChatPromptTemplate` 统一管理。改动 prompt 不必再碰业务代码。

扩展点：
- 同一图表生成任务可换不同版本 prompt（A/B 测试用）
- 用 ``partial()`` 注入运行时变量
- 用 ``MessagesPlaceholder`` 留出多轮对话位置（未来扩展）
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate


# ========================================================================
# Chart 生成 — 系统提示词（默认；用户可在配置页覆盖）
# ========================================================================

DEFAULT_CHART_SYSTEM_PROMPT = """你是一名资深的 Apache ECharts 图表工程师。

【输出格式 — 双模式】
输出一个 JSON 对象，**只**包含以下三个字段（按需选择，option 和 code 至少一个非空）：

1) **"code"**（推荐）：完整可执行的 JS 代码段 —— 定义 ``const option = {...}`` 并调用 ``chart.setOption(option)``；
   option 内可使用任意 ECharts 回调函数（formatter / label.formatter / tooltip.formatter / axisLabel.formatter 等），
   让渲染效果更灵活。
2) **"option"**：严格 JSON 配置对象（不可包含函数，回调字段用字符串模板，如 ``'{b}: {c}'``）。
   仅在无需自定义函数时使用。
3) **"content"**：30-80 字中文文字解释（最大/最小/趋势/对比），不要复述数据、不要 markdown、不要 emoji。

【硬约束 — 违反任何一条视为不合格】
1) 整个 JSON 首字符必须是 ``{``、末字符必须是 ``}``，不写 Markdown 代码块、不写任何前后缀文字。
2) ``code`` 必须是**自包含可执行**的 JS（不能依赖外部变量、不能 require / import）；变量名用 ``option``；
   ``code`` 内必须调用 ``chart.setOption(option)``。
3) ECharts 配置（xAxis / yAxis / series / color 等）按数据真实填写，不要编造；
   数值用 number（不要加引号）；时间用 type:'time'；类别用 type:'category' + data 数组。
4) ``content`` 不超 80 字、不复述数据、不用 markdown / emoji。
5) **绝对禁止**把 ``{`` / ``}`` 写成 ``{{`` / ``}}`` —— 它们是合法 JSON 字符，不要按 f-string / 模板语法把它们当成变量转义。
   - ✅ 正确：``"option": {"series": []}``
   - ❌ 错误：``"option": {{"series": []}}``
   - ❌ 错误：``{{ "option": {...}, "content": "..." }}``
6) **sankey / graph / tree / sunburst / treemap / boxplot 等节点的 series.data 必须传对象数组**
   ``[{name: 'A'}, {name: 'B'}]``，**不要写字符串数组** ``['A', 'B']``
   （ECharts 5.x 字符串数组不会被当节点处理，会导致图表不渲染）。
7) **series.data 与 series.links/edges 引用要配对** —— sankey / graph 的 ``links``/``edges`` 里
   ``source``/``target`` 必须是 ``data`` 中某个节点的 name；不在 data 里的 source/target 会被静默忽略。
8) **option 模式（纯 JSON）**里的 formatter 只能用字符串模板（如 ``'{b}: {c}'``），**不能写 JS 函数**（JSON 不支持函数）。
   **code 模式**则相反 —— 可使用完整 JS 函数（箭头函数、模板字符串、toLocaleString、Intl.NumberFormat 等都可以）。

【质量约束】
- 标题 / 副标题 / 图例 / 坐标轴名 / 系列名 与数据列名 / 语义保持一致。
- 颜色：用户没指定时用 ECharts 默认调色板（#5470c6 #91cc75 #ee6666 #73a0fa #fac858 #3ba272）。
- 标题 / 副标题可以留空。

【code 模式示例 — 自定义 formatter（推荐）】
{
  "code": "const option = { tooltip: { trigger: 'axis', formatter: function(params){ return params.map(p => p.marker + p.seriesName + ': ' + p.value.toLocaleString() + '元').join('<br/>') } }, xAxis: { type: 'category', data: ['1月','2月'] }, yAxis: { type: 'value' }, series: [{ name: '销售额', type: 'bar', data: [120000, 132500], label: { show: true, formatter: '{c}元' } }] }; chart.setOption(option);",
  "content": "销售额1-2月呈上升趋势，2月达13.25万元。"
}

【option 模式示例 — 标准配置无回调】
{
  "option": {
    "title": {"text": "某月销售额"},
    "tooltip": {"trigger": "axis"},
    "xAxis": {"type": "category", "data": ["1月", "2月"]},
    "yAxis": {"type": "value"},
    "series": [{"name": "销售额", "type": "bar", "data": [120, 132]}]
  },
  "content": "2月销售额132高于1月的120，环比增长10%。"
}

【默认推荐】使用 **code 模式** —— 让渲染效果更灵活、可使用任意 ECharts 回调函数。
"""


# ========================================================================
# Chart 生成 — User prompt 构造（运行时直接拼接，性能 > 模板渲染）
# ========================================================================


def build_chart_user_prompt(
    prompt: str,
    data: Optional[Dict[str, Any]],
    chart_type: str,
    style_hint: Optional[Dict[str, Any]],
    knowledge: Dict[str, str],
) -> str:
    """拼装图表生成阶段的 user prompt（流式生成时直接 yield 文本即可）。"""
    pieces: List[str] = [f"请使用 Apache ECharts 绘制一张「{chart_type}」类型图表。"]

    if prompt:
        pieces.append(f"用户需求：{prompt}")

    rows: List[Dict[str, Any]] = []
    stats_text = ""
    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            column_names = [str(c.get("name") or "") for c in raw_cols]
        else:
            column_names = [str(c) for c in raw_cols]

        rows = data["rows"]
        # 统计（懒加载避免循环引用）
        from llm_client import compute_column_stats
        stats_text = compute_column_stats(rows, column_names)
        if stats_text:
            pieces.append(f"【数据统计摘要 (共 {len(rows)} 行)】\n{stats_text}")

        # 数据量大时截断到前 100 行
        shown = rows[:100] if len(rows) > 100 else rows
        pieces.append("数据 JSON：")
        pieces.append(json.dumps(shown, ensure_ascii=False))
        if len(rows) > 100:
            pieces.append(
                f"（注：原始数据共 {len(rows)} 行，这里仅发送前 100 行用于演示；"
                f"生成代码时请按完整结构保留。）"
            )

        if data.get("summary"):
            pieces.append(f"数据集摘要：{data['summary']}")
        if data.get("notes") and data.get("notes") not in ("", "无"):
            pieces.append(f"数据整理说明：{data['notes']}")

    if style_hint:
        pieces.append(f"样式偏好：{json.dumps(style_hint, ensure_ascii=False)}")

    pieces.append(
        "请严格按 system 给定的 JSON schema 输出（不要写 Markdown 代码块、不要任何前后缀文字），"
        "其中：\n"
        "1) option / code 二选一即可 —— 推荐 code 模式（可使用任意 ECharts 回调函数）；\n"
        "2) xAxis / yAxis / legend 的内容与数据列名一致；\n"
        "3) 标题/副标题可以留空；\n"
        "4) content 字段填 30-80 字中文文字解释该图表表达的核心信息（最大/最小/趋势/对比），"
        "不要复述数据，不要 markdown 格式。"
    )

    if knowledge:
        pieces.append("【ECharts 配置项指导】")
        for section_name, content in knowledge.items():
            pieces.append(f"-- {section_name} --")
            pieces.append(content)

    base = "\n\n".join(pieces)

    # 预处理注记：让 LLM 知道数据已被预处理（精度提示）
    if data and isinstance(data.get("preprocess"), dict):
        pp = data["preprocess"]
        actions = [
            a.get("action")
            for a in (pp.get("applied") or [])
            if a.get("action") and not a.get("skipped")
        ]
        actions = [a for a in actions if a]
        if actions:
            base += (
                "\n\n【数据预处理已应用】以下规则已对数据生效（前端已按结果展示，"
                "请在 ECharts option 的 tooltip / axisLabel / series.label 等展示处使用与数据一致的精度"
                "（例如 toFixed(2)），避免出现 1.2300000001 之类的尾数）：\n"
                + "\n".join(f"- {a}" for a in actions)
            )

    return base


# ========================================================================
# Chart 类型选择
# ========================================================================

CHART_TYPE_SYSTEM = (
    "你是专业的数据可视化助手。请根据用户的需求和数据，从可选 ECharts 图表类型列表中选一个最合适的，"
    "并用中文一句话说明理由。"
)

CHART_TYPE_USER_TEMPLATE = """用户需求：{prompt_or_default}{data_desc}

可选图表类型：{allowed_list}

请给出推荐图表类型与理由。
"""


def chart_type_prompt_template() -> ChatPromptTemplate:
    """图表类型推荐的 ChatPromptTemplate。"""
    return ChatPromptTemplate.from_messages([
        ("system", CHART_TYPE_SYSTEM),
        ("human", CHART_TYPE_USER_TEMPLATE),
    ])


def format_data_for_type_prompt(
    prompt: str,
    data: Optional[Dict[str, Any]],
    allowed_list: List[str],
) -> Dict[str, str]:
    """构造图表类型推荐 prompt 的入参。"""
    data_desc = ""
    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            cols = ", ".join(str(c.get("name") or "") for c in raw_cols)
        else:
            cols = ", ".join(str(c) for c in raw_cols)
        sample = json.dumps(data["rows"][:3], ensure_ascii=False)
        extra = ""
        if data.get("summary"):
            extra = f"\n数据集摘要：{data['summary']}"
        data_desc = f"\n数据字段：{cols}\n样例：{sample}\n行数：{len(data['rows'])}{extra}"

    return {
        "prompt_or_default": prompt or "根据提供的数据自动生成合适的图表",
        "data_desc": data_desc,
        "allowed_list": ", ".join(sorted(allowed_list)),
    }


# ========================================================================
# Data 整理
# ========================================================================

DATA_UNDERSTANDING_SYSTEM = """你是一名资深的数据整理助手，擅长把不规范的真实数据整理成规范的表格。

用户上传的数据可能存在以下问题，代码解析常常只能得到粗略甚至错误的草稿：
- 多级表头、合并单元格、中英文混排
- 数值单元格带单位（"万"、"%"、"元"）、带千分位（"1,234"）
- 合计行、空行、表头重复行
- 转置表、行被错位、列缺失
- 字段命名含前后空格、不可见字符、同义不同名

请你结合「原始文本片段」与「代码解析草稿」完成整理工作，严格按 JSON Schema 输出，
**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀。

约束：
1) rows 中所有 key 必须与 columns 中的 name 完全一致。
2) number 类型必须输出为 JSON number（不要带引号、不要带单位）；date 输出 "YYYY-MM-DD" 字符串；
   boolean 输出 true/false；其它一律 string。
3) 行数应与原始数据有效行一致（不要人为缩减，也不要造数据）。如果原始行数很多，
   至少给出前 30 行作为示例，并完整保留所有列结构。
4) 严格过滤：合计行/总计行/空行/重复表头一律剔除。
5) 若某列无意义或纯粹是噪声，role 填 "ignore"，但仍保留在 columns 里、rows 中可填空字符串。
6) 不要在 JSON 之外输出任何字符。
"""

DATA_UNDERSTANDING_USER_TEMPLATE = """【用户意图（可选）】
{hint}

【代码解析草稿】
列名：{cols}
共 {count} 行；前 {sample_n} 行样例：
{sample}

【列统计摘要（>20 行才生成）】
{stats}

【原始数据片段（最多 {raw_chars} 字符）】
{raw}

请输出整理后的 JSON。
"""


def data_understanding_prompt_template() -> ChatPromptTemplate:
    """数据整理 ChatPromptTemplate。"""
    return ChatPromptTemplate.from_messages([
        ("system", DATA_UNDERSTANDING_SYSTEM),
        ("human", DATA_UNDERSTANDING_USER_TEMPLATE),
    ])


__all__ = [
    "DEFAULT_CHART_SYSTEM_PROMPT",
    "build_chart_user_prompt",
    "CHART_TYPE_SYSTEM",
    "chart_type_prompt_template",
    "format_data_for_type_prompt",
    "DATA_UNDERSTANDING_SYSTEM",
    "DATA_UNDERSTANDING_USER_TEMPLATE",
    "data_understanding_prompt_template",
]
