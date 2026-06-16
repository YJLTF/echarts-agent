#!/usr/bin/env python3
"""数据理解与整理的 Prompt 模板。"""
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate

SYSTEM_PROMPT = """你是一名资深的数据整理助手，擅长把不规范的真实数据整理成规范的表格。

用户上传的数据可能存在以下问题，代码解析常常只能得到粗略甚至错误的草稿：
- 多级表头、合并单元格、中英文混排
- 数值单元格带单位（"万"、"%"、"元"）、带千分位（"1,234"）
- 合计行、空行、表头重复行
- 转置表、行被错位、列缺失
- 字段命名含前后空格、不可见字符、同义不同名

请你结合「原始文本片段」与「代码解析草稿」完成整理工作，严格按下面的 JSON Schema 输出，
**只输出一个 JSON 对象**，不要任何解释、Markdown 代码块、注释或前后缀：

{
  "columns": [
    {
      "name": "规范化后的列名（中文保持中文，英文保持英文；去空格/单位）",
      "type": "string | number | date | boolean 之一",
      "role": "category | value | time | series | label | ignore 之一",
      "description": "该列含义的一行中文说明"
    }
  ],
  "rows": [
    {"规范化列名1": 值, "规范化列名2": 值, ...}
  ],
  "summary": "一句话中文总结这个数据集的内容",
  "notes": "做了哪些清洗动作（如去单位/剔合计行/合并表头等）；若代码草稿无误可写 '无'"
}

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


USER_PROMPT_TEMPLATE = """【用户意图（可选）】
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


def build_data_understanding_prompt(
    hint: str,
    cols: str,
    count: int,
    sample_n: int,
    sample: str,
    stats: str,
    raw_chars: int = 6000,
    raw: str = "",
) -> ChatPromptTemplate:
    """构建数据理解与整理的 Prompt 模板。"""
    system_msg = SystemMessagePromptTemplate.from_template(SYSTEM_PROMPT)
    human_msg = HumanMessagePromptTemplate.from_template(USER_PROMPT_TEMPLATE)

    return ChatPromptTemplate.from_messages([system_msg, human_msg])


def format_data_understanding_input(
    hint: str = "",
    cols: str = "",
    count: int = 0,
    sample_rows: list = None,
    stats: str = "",
    raw: str = "",
    raw_chars: int = 6000,
) -> dict:
    """格式化数据理解 Prompt 的输入参数。"""
    sample_n = len(sample_rows) if sample_rows else 0
    sample_text = ""
    if sample_rows:
        import json
        sample_text = json.dumps(sample_rows[:8], ensure_ascii=False, indent=2)

    return {
        "hint": hint or "（无）",
        "cols": cols or "(无)",
        "count": count,
        "sample_n": sample_n,
        "sample": sample_text or "(无)",
        "stats": stats or "（样本太小，无需统计）",
        "raw_chars": raw_chars,
        "raw": (raw or "").strip()[:raw_chars] if raw else "(无)",
    }
