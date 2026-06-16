"""prompts —— Prompt 注册中心。

把各模块的 system prompt / user prompt 模板统一汇聚在这里，
方便上层 Chain 做版本化、A/B 测试、或切换风格。

使用方式::

    from prompts import DATA_UNDERSTANDING_SYSTEM, build_data_understanding_input
    from prompts import CHART_GEN_SYSTEM, build_chart_gen_input
    from prompts import CHART_TYPE_PROMPT

若以后要加一个新图表类型（例如 3D bar），只需要在对应模块
（:mod:`prompts.chart_generation`）追加模板，再在这里 re-export 即可。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------- data_understanding ----------

try:
    from prompts.data_understanding import (
        SYSTEM_PROMPT as DATA_UNDERSTANDING_SYSTEM,
        USER_PROMPT_TEMPLATE as DATA_UNDERSTANDING_USER_TEMPLATE,
        format_data_understanding_input as build_data_understanding_input,
    )
    _DATA_OK = True
except Exception:
    DATA_UNDERSTANDING_SYSTEM = ""
    DATA_UNDERSTANDING_USER_TEMPLATE = ""
    def build_data_understanding_input(inputs: Dict[str, Any]) -> Dict[str, Any]:
        return dict(inputs)
    _DATA_OK = False


# ---------- chart_type ----------

try:
    from prompts.chart_type import (
        SYSTEM_PROMPT as CHART_TYPE_SYSTEM,
        build_chart_type_prompt,
    )
    _CHART_TYPE_OK = True
except Exception:
    CHART_TYPE_SYSTEM = ""
    def build_chart_type_prompt(*args: Any, **kwargs: Any) -> str:
        return " ".join(map(str, args))
    _CHART_TYPE_OK = False


# ---------- chart_generation ----------

try:
    from prompts.chart_generation import (
        SYSTEM_PROMPT as CHART_GEN_SYSTEM,
        build_chart_user_prompt,
    )
    _CHART_GEN_OK = True
except Exception:
    CHART_GEN_SYSTEM = "你是一个专业的 ECharts 图表生成助手。"
    def build_chart_user_prompt(**kwargs: Any) -> str:
        return str(kwargs.get("prompt", ""))
    _CHART_GEN_OK = False


# ---------- 诊断（供启动时打印） ----------

def status() -> Dict[str, bool]:
    """返回各 prompt 模块的导入状态，方便排查。"""
    return {
        "data_understanding": _DATA_OK,
        "chart_type": _CHART_TYPE_OK,
        "chart_generation": _CHART_GEN_OK,
    }


__all__ = [
    "DATA_UNDERSTANDING_SYSTEM",
    "DATA_UNDERSTANDING_USER_TEMPLATE",
    "build_data_understanding_input",
    "CHART_TYPE_SYSTEM",
    "build_chart_type_prompt",
    "CHART_GEN_SYSTEM",
    "build_chart_user_prompt",
    "status",
]
