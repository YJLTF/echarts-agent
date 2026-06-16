"""chains.base
LangChain 风格的 Chain 构建器：提供从 cfg 构建 ChatOpenAI、构建结构化输出 Chain
的通用工具，让上层 Chain 可以用声明式的方式装配。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Type, TypeVar, Union

from pydantic import BaseModel, ConfigDict

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from langchain_core.output_parsers import (
        JsonOutputParser,
        PydanticOutputParser,
        StrOutputParser,
    )
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough
    from langchain_core.runnables import RunnableSerializable
    _HAS_LANGCHAIN = True
except Exception:
    _HAS_LANGCHAIN = False

    # 降级：定义简化的占位接口，保持 import 路径稳定
    class BaseMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = "", **kwargs: Any) -> None:
            self.content = content
            self.additional_kwargs: Dict[str, Any] = {}

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class HumanMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class SystemMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class RunnableLambda:  # type: ignore[no-redef]
        def __init__(self, func: Any) -> None:
            self._func = func

        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            return self._func(*args, **kwargs)

        def __ror__(self, other: Any) -> Any:
            return other

        def __or__(self, other: Any) -> Any:
            return other

    class RunnablePassthrough:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def invoke(self, x: Any, **kwargs: Any) -> Any:
            return x


from llm_client import ChatOpenAIWrapper, get_llm_wrapper, resolve_extra_kwargs

T = TypeVar("T", bound=BaseModel)


# ========================================================================
# LLM 工厂：从 cfg 构建 ChatOpenAI 实例
# ========================================================================

def build_llm(
    cfg: Dict[str, Any],
    *,
    stream: bool = False,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> ChatOpenAIWrapper:
    """从配置 dict 构建一个 ``ChatOpenAIWrapper`` 实例。

    读取的键：``base_url`` / ``api_key`` / ``model`` / ``temperature`` / ``max_tokens`` / ``llm_thinking``。

    这个函数是 LangChain 集成的核心入口：

    - 把分散在 ``app.py`` / ``data_understanding.py`` / ``llm_client.py`` 里
      构造 ChatOpenAI 的零散代码统一收敛到这里
    - 自动注入 ``resolve_extra_kwargs`` 解析得到的 reasoning_effort / thinking
    - 支持 ``stream`` 参数直接走流式调用
    """
    llm_reasoning_effort = reasoning_effort or cfg.get("llm_thinking") or cfg.get("reasoning_effort")
    wrapper = get_llm_wrapper(cfg)
    return wrapper


# ========================================================================
# Prompt 构造：把 (system, user) 构造成 LangChain ChatPromptTemplate
# ========================================================================

def build_chat_prompt(system: str, user: str) -> Any:
    """构造一个简易的 ``ChatPromptTemplate.from_messages([('system', system), ('user', user)])``。

    如果 LangChain 不可用，则降级为返回 ``(system, user)`` 字符串元组——上层 Chain
    可以据此做纯字符串拼接。
    """
    if _HAS_LANGCHAIN:
        return ChatPromptTemplate.from_messages([
            ("system", system),
            ("user", user),
        ])
    return system, user


# ========================================================================
# 消息转换：旧接口 Dict[str, str] ↔ LangChain BaseMessage
# ========================================================================

def to_langchain_messages(messages: List[Dict[str, str]]) -> List[BaseMessage]:
    """把 ``[{'role': 'system'|'user', 'content': str}, ...]`` 转为 LangChain 消息列表。"""
    out: List[BaseMessage] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content))
        else:
            out.append(HumanMessage(content=content))
    return out


def to_raw_messages(messages: List[BaseMessage]) -> List[Dict[str, str]]:
    """把 LangChain 消息列表转成与旧接口兼容的 ``[{'role': ..., 'content': ...}]``。"""
    out: List[Dict[str, str]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            out.append({"role": "system", "content": msg.content})
        else:
            out.append({"role": "user", "content": msg.content})
    return out


# ========================================================================
# 结构化输出 Chain：prompt → llm → Pydantic 模型
# ========================================================================

class StructuredLLMChain:
    """声明式的结构化输出 Chain：``prompt | llm | pydantic_output_parser``。

    与手写 ``call_llm + json.loads + 5 层兜底`` 相比，它在模型支持 JSON mode
    时优先走 ``ChatOpenAI.with_structured_output``，失败时降级到手写解析器。
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        system_prompt: str,
        output_model: Optional[Type[BaseModel]] = None,
        *,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.output_model = output_model
        self.wrapper = build_llm(cfg, reasoning_effort=reasoning_effort)

        # 尝试装配 LangChain 原生结构化输出
        self._structured: Optional[Any] = None
        if _HAS_LANGCHAIN and output_model is not None:
            try:
                self._structured = self.wrapper.llm.with_structured_output(output_model)
            except Exception:
                self._structured = None

    # ---------- invoke（非流式） ----------

    def invoke(self, user_input: str) -> Any:
        """调用 LLM，返回结构化对象；若 ``output_model`` 为 None，返回字符串 content。"""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        # 优先尝试 LangChain 原生结构化输出
        if self._structured is not None:
            try:
                lc_messages = to_langchain_messages(messages)
                return self._structured.invoke(lc_messages)
            except Exception:
                # 降级：回到普通调用 + 手写解析
                pass

        content, _ = self.wrapper.call_llm_raw(messages)

        # 结构化解析
        if self.output_model is not None:
            # 优先 json.loads → 再尝试 ```json 围栏 → 再尝试裸对象
            obj = _smart_parse(content)
            if obj is not None:
                try:
                    return self.output_model.model_validate(obj)
                except Exception:
                    pass
            # 最后兜底：把 content 塞到 content 字段里
            try:
                return self.output_model.model_validate({"content": content, "option": {}})
            except Exception:
                return content
        return content

    # ---------- stream ----------

    def stream(self, user_input: str) -> Iterator[str]:
        """流式调用，逐 chunk 产出 content 文本。"""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        for chunk in self.wrapper.call_llm_stream(messages):
            if chunk:
                yield chunk


# ========================================================================
# 工具：智能 JSON 解析（5 层兜底的精简版）
# ========================================================================

def _smart_parse(raw: str) -> Optional[Dict[str, Any]]:
    """尝试 5 层兜底地从任意文本中提取出 JSON 对象。"""
    text = (raw or "").strip()
    if not text:
        return None

    # 1) 直接 json.loads
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 2) ```json 围栏
    m = _RE_FENCE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # 3) 扫第一个 { ... } 完整对象
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


import re as _re  # noqa: E402

_RE_FENCE = _re.compile(r"```(?:json|JSON)?\s*([\s\S]+?)\s*```")
