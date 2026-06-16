#!/usr/bin/env python3
"""图表生成的 Prompt 模板。"""
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate

SYSTEM_PROMPT = """你是一个专业的 ECharts 图表生成助手。请根据用户需求和提供的数据，生成符合要求的 ECharts 配置。"""


def build_chart_generation_prompt() -> ChatPromptTemplate:
    """构建图表生成的 Prompt 模板。"""
    system_msg = SystemMessagePromptTemplate.from_template(SYSTEM_PROMPT)
    human_msg = HumanMessagePromptTemplate.from_template("{user_prompt}")
    return ChatPromptTemplate.from_messages([system_msg, human_msg])


def build_chart_user_prompt(
    prompt: str,
    data: dict = None,
    chart_type: str = "bar",
    style_hint: dict = None,
    knowledge: dict = None,
    preprocess_info: dict = None,
) -> str:
    """构建图表生成的用户 prompt 内容。"""
    import json

    pieces = []
    pieces.append(f"请使用 Apache ECharts 绘制一张「{chart_type}」类型图表。")
    if prompt:
        pieces.append(f"用户需求：{prompt}")

    if data and data.get("rows"):
        # 兼容两种列描述：纯字符串列表 / 带 schema 的对象列表
        raw_cols = data.get("columns") or []
        if raw_cols and isinstance(raw_cols[0], dict):
            column_names = [str(c.get("name") or "") for c in raw_cols]
            schema_lines = [
                f"- {c.get('name')} ({c.get('type','string')}/{c.get('role','value')}): {c.get('description','')}"
                for c in raw_cols
            ]
            pieces.append("数据 schema：\n" + "\n".join(schema_lines))
        else:
            column_names = [str(c) for c in raw_cols]
        rows = data["rows"]
        pieces.append("数据字段：" + ", ".join(column_names))

        # 整体统计（>20 行才发）
        from llm_client import compute_column_stats
        stats_text = compute_column_stats(rows, column_names)
        if stats_text:
            pieces.append(f"【数据统计摘要 (共 {len(rows)} 行)】\n{stats_text}")

        # 样本行（>100 行只发前 100）
        if len(rows) > 100:
            shown = rows[:100]
            pieces.append(f"数据共 {len(rows)} 行，仅发送前 100 行用于演示；"
                          f"请在生成代码时按相同字段保留完整结构：")
        else:
            shown = rows
        pieces.append("数据 JSON：")
        pieces.append(json.dumps(shown, ensure_ascii=False))

        if data.get("summary"):
            pieces.append(f"数据集摘要：{data['summary']}")
        if data.get("notes") and data.get("notes") not in ("", "无"):
            pieces.append(f"数据整理说明：{data['notes']}")

    if style_hint:
        pieces.append(f"样式偏好：{json.dumps(style_hint, ensure_ascii=False)}")

    pieces.append(
        "请严格按 response_format 给定的 JSON schema 输出（不要写 Markdown 代码块、不要任何前后缀文字），"
        "其中：\n"
        "1) option 是完整的 ECharts 配置对象（title/tooltip/legend/grid/xAxis/yAxis/series/color 等），"
        "series.data 填入真实数值；\n"
        "2) xAxis/yAxis/legend 的内容与数据列名一致；\n"
        "3) 标题/副标题可以留空；\n"
        "4) 禁止 JS 函数字面量（JSON 不支持函数），自定义 formatter 用 ECharts 字符串模板"
        "（如 '{b}: {c}'），自定义配色用 series 顶层 color: [...] 数组或省略；\n"
        "5) content 字段填 30-80 字中文文字解释该图表表达的核心信息（最大/最小/趋势/对比），"
        "不要复述数据，不要 markdown 格式。"
    )

    if knowledge:
        pieces.append("【ECharts 配置项指导】")
        for section_name, content in knowledge.items():
            pieces.append(f"-- {section_name} --")
            pieces.append(content)

    if preprocess_info and preprocess_info.get("applied"):
        actions = [
            a.get("action")
            for a in preprocess_info.get("applied", [])
            if a.get("action") and not a.get("skipped")
        ]
        actions = [a for a in actions if a]
        if actions:
            pieces.append(
                "\n\n【数据预处理已应用】以下规则已对数据生效（前端已按结果展示，"
                "请在 ECharts option 的 tooltip / axisLabel / series.label 等展示处使用与数据一致的精度"
                "（例如 toFixed(2)），避免出现 1.2300000001 之类的尾数）：\n"
                + "\n".join(f"- {a}" for a in actions)
            )

    return "\n\n".join(pieces)
