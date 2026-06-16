"""对话记忆模块（LangChain 风格）。

对外提供两套 API：

1. **简易 API**：``get_memory`` / ``add_user_message`` / ``add_ai_message`` — 与旧代码兼容
2. **LangChain 风格**：``LangChainChatMessageHistory`` — 实现类似
   ``BaseChatMessageHistory`` 的 ``messages`` / ``add_message`` / ``clear`` 方法，
   可直接在 :class:`chains.base.StructuredLLMChain` 中使用

**注意**：真正的 ``langchain_core.chat_history.BaseChatMessageHistory`` 需要
``langchain-core >= 0.1.0``；本模块在没有 LangChain 的情况下提供了兼容实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Any


# ========================================================================
# 简易消息类：不依赖 langchain
# ========================================================================

@dataclass
class SimpleMessage:
    """一条简化的对话消息，与 LangChain 的 ``BaseMessage`` 保持概念对齐。

    ``role`` ∈ ``{"user", "assistant", "system", "function", "tool"}``
    """

    role: str
    content: str
    additional_kwargs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, str]:
        """与旧接口兼容：返回 ``{"role", "content"}``。"""
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "SimpleMessage":
        return cls(
            role=str(obj.get("role", "user")),
            content=str(obj.get("content", "")),
            additional_kwargs={k: v for k, v in obj.items() if k not in ("role", "content")},
        )


# ========================================================================
# 全局 memory 存储（进程内）
# ========================================================================

_memory_store: Dict[str, List[SimpleMessage]] = {}


def _ensure_store(session_id: str) -> List[SimpleMessage]:
    if session_id not in _memory_store:
        _memory_store[session_id] = []
    return _memory_store[session_id]


# ========================================================================
# 简易 API（与旧代码兼容）
# ========================================================================

def get_memory(session_id: str = "default") -> List[Dict[str, str]]:
    """返回指定会话的消息列表（``[{"role", "content"}, ...]``）。"""
    return [m.to_dict() for m in _ensure_store(session_id)]


def clear_memory(session_id: str = "default") -> None:
    """清除指定会话的记忆。"""
    _ensure_store(session_id).clear()


def get_chat_history(session_id: str = "default") -> List[Dict[str, str]]:
    """同 :func:`get_memory`。"""
    return get_memory(session_id)


def add_user_message(session_id: str, message: str) -> None:
    """添加一条用户消息。"""
    _ensure_store(session_id).append(SimpleMessage(role="user", content=message))


def add_ai_message(session_id: str, message: str) -> None:
    """添加一条 AI 消息。"""
    _ensure_store(session_id).append(SimpleMessage(role="assistant", content=message))


def add_system_message(session_id: str, message: str) -> None:
    """添加一条 system 消息。"""
    _ensure_store(session_id).append(SimpleMessage(role="system", content=message))


# ========================================================================
# LangChain 风格 API
# ========================================================================

class LangChainChatMessageHistory:
    """类似 ``langchain_core.chat_history.BaseChatMessageHistory`` 的接口。

    使用方式::

        history = LangChainChatMessageHistory(session_id="default")
        history.add_user_message("你好")
        history.add_ai_message("你好！")
        print(history.messages)  # List[SimpleMessage]
        history.clear()

    若本进程装了 ``langchain_core``，你可以轻松把它当作 ``BaseChatMessageHistory``
    一样使用。你只需要把 ``SimpleMessage`` 转成真正的 ``HumanMessage`` /
    ``AIMessage`` / ``SystemMessage``：::

        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        def to_langchain_messages(messages: list[SimpleMessage]):
            mapping = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}
            return [mapping[m.role](content=m.content) for m in messages]
    """

    def __init__(self, session_id: str = "default", max_messages: Optional[int] = None):
        self.session_id = session_id
        self.max_messages = max_messages

    # ---------- 读 ----------

    @property
    def messages(self) -> List[SimpleMessage]:
        """返回原始消息列表（尾部受 max_messages 截断）。"""
        raw = _ensure_store(self.session_id)
        if self.max_messages is not None and len(raw) > self.max_messages:
            raw = raw[-self.max_messages:]
        return raw

    # ---------- 写 ----------

    def add_message(self, message: SimpleMessage) -> None:
        _ensure_store(self.session_id).append(message)

    def add_user_message(self, message: str) -> None:
        self.add_message(SimpleMessage(role="user", content=message))

    def add_ai_message(self, message: str) -> None:
        self.add_message(SimpleMessage(role="assistant", content=message))

    def add_system_message(self, message: str) -> None:
        self.add_message(SimpleMessage(role="system", content=message))

    def save_context(self, user_input: str, output: str) -> None:
        """保存一对 (user, assistant) 的对话上下文。"""
        self.add_user_message(user_input)
        self.add_ai_message(output)

    def clear(self) -> None:
        clear_memory(self.session_id)

    # ---------- 辅助 ----------

    def to_raw_list(self) -> List[Dict[str, str]]:
        """返回 ``[{"role", "content"}, ...]``，方便传入 LLM。"""
        return [m.to_dict() for m in self.messages]

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self) -> Iterable[SimpleMessage]:
        return iter(self.messages)


# ========================================================================
# 面向对象别名（与旧代码兼容）
# ========================================================================

class ChatMemory(LangChainChatMessageHistory):
    """``LangChainChatMessageHistory`` 的同义别名，保持与旧代码的 ``ChatMemory`` 一致。"""

    def get_history(self) -> List[Dict[str, str]]:
        return self.to_raw_list()
