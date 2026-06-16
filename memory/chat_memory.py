#!/usr/bin/env python3
"""对话记忆模块 - 简单的内存管理。

注意：完整的 LangChain Memory 集成需要 langchain-core >= 0.1.0 的特定版本。
此模块提供了基本的内存存储功能。
"""
from typing import Dict, List, Optional, Any


# 全局 memory 存储（实际生产环境应使用持久化存储）
_memory_store: Dict[str, List[Dict[str, str]]] = {}


def get_memory(session_id: str = "default") -> List[Dict[str, str]]:
    """获取指定会话的记忆列表。"""
    if session_id not in _memory_store:
        _memory_store[session_id] = []
    return _memory_store[session_id]


def clear_memory(session_id: str = "default") -> None:
    """清除指定会话的记忆。"""
    if session_id in _memory_store:
        _memory_store[session_id] = []


def get_chat_history(session_id: str = "default") -> List[Dict[str, str]]:
    """获取指定会话的对话历史。"""
    return get_memory(session_id)


def add_user_message(session_id: str, message: str) -> None:
    """添加用户消息到记忆。"""
    memory = get_memory(session_id)
    memory.append({"role": "user", "content": message})


def add_ai_message(session_id: str, message: str) -> None:
    """添加 AI 消息到记忆。"""
    memory = get_memory(session_id)
    memory.append({"role": "assistant", "content": message})


class ChatMemory:
    """对话记忆管理器（面向对象接口）。"""

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id

    def add_user_message(self, message: str) -> None:
        """添加用户消息。"""
        add_user_message(self.session_id, message)

    def add_ai_message(self, message: str) -> None:
        """添加 AI 消息。"""
        add_ai_message(self.session_id, message)

    def get_history(self) -> List[Dict[str, str]]:
        """获取对话历史。"""
        return get_chat_history(self.session_id)

    def clear(self) -> None:
        """清除记忆。"""
        clear_memory(self.session_id)

    def save_context(self, user_input: str, output: str) -> None:
        """保存对话上下文。"""
        self.add_user_message(user_input)
        self.add_ai_message(output)
