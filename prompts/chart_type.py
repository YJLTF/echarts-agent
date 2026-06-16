#!/usr/bin/env python3
"""图表类型选择的 Prompt 模板。"""
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate

SYSTEM_PROMPT = """你是一个专业的数据可视化助手，请根据用户的需求与数据，推荐一个最合适的 ECharts 图表类型。

请只回复一个英文单词，从以下类型中选择：bar, line, pie, scatter, radar, gauge, funnel, candlestick, heatmap, sunburst, treemap, sankey, boxplot, pictorialBar, effectScatter。
随后用一行中文简短解释选择理由。

示例输出：
bar
用于比较不同类别之间的数值大小，适合该场景的分类数据对比。
"""


def build_chart_type_prompt() -> ChatPromptTemplate:
    """构建图表类型选择的 Prompt 模板。"""
    system_msg = SystemMessagePromptTemplate.from_template(SYSTEM_PROMPT)
    human_msg = HumanMessagePromptTemplate.from_template(
        "用户需求：{prompt}\n\n数据字段：{columns}\n数据样例：{sample}\n{extra}\n\n请给出推荐图表类型与理由。"
    )
    return ChatPromptTemplate.from_messages([system_msg, human_msg])


def format_chart_type_input(
    prompt: str,
    data: dict = None,
    hint: str = "",
) -> dict:
    """格式化图表类型选择 Prompt 的输入参数。"""
    import json

    columns = ""
    sample = ""
    extra = ""

    if data and data.get("rows"):
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            columns = ", ".join(str(c.get("name") or "") for c in raw_cols)
        else:
            columns = ", ".join(str(c) for c in raw_cols)

        sample_rows = data["rows"][:3]
        sample = json.dumps(sample_rows, ensure_ascii=False)

        if data.get("summary"):
            extra = f"\n数据集摘要：{data['summary']}"

    extra = extra + f"\n用户指定图表类型：{hint}" if hint else extra

    return {
        "prompt": prompt or "根据提供的数据自动生成合适的图表",
        "columns": columns,
        "sample": sample,
        "extra": extra,
    }
