"""
把 llm 模块接到 Web 服务上。

这一层只做三件事：

  1. **决定用哪个后端**：读环境变量，没装模型也能跑（规则后端兜底）。
     后端初始化失败不应该让整个服务起不来——模型是加分项，不是命门。
  2. **为每个 user_id 维持一个 Router**：对话历史与轮次状态挂在这里。
  3. **让 Session 直接引用 api 层的共享状态**：Session.log / Session.weights
     直接指向 api 层的那两个对象，于是「在聊天里记的一餐」和「在表单里记的一餐」
     是同一份数据，不需要来回同步；只有 profile 因为会被整体替换，需要写回一次。

环境变量：

    SEEKMEAL_LLM_BACKEND   rule / openai / llamacpp   （默认 rule）
    SEEKMEAL_LLM_MODEL     模型名（openai）或 GGUF 路径（llamacpp）
    SEEKMEAL_LLM_BASE_URL  OpenAI 兼容服务地址，默认 http://127.0.0.1:1234/v1
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from .backend import Backend, RuleBackend, make_backend
from .router import ChatResult, Router
from .tools import Engine, Session

ENV_BACKEND = "SEEKMEAL_LLM_BACKEND"
ENV_MODEL = "SEEKMEAL_LLM_MODEL"
ENV_BASE_URL = "SEEKMEAL_LLM_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"


@dataclass
class BackendInfo:
    """给 /health 用的后端自述。"""

    kind: str            # 实际生效的类型：rule / openai / llamacpp
    name: str            # 后端自报名（含模型名）
    requested: str       # 环境变量里要求的类型
    model: str = ""
    note: str = ""       # 降级原因等

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "requested": self.requested,
                "model": self.model, "note": self.note}


def backend_from_env() -> tuple[Backend, BackendInfo]:
    """按环境变量构造后端。任何失败都降级到规则后端，并把原因记在 note 里。"""
    requested = (os.environ.get(ENV_BACKEND) or "rule").strip().lower()
    model = (os.environ.get(ENV_MODEL) or "").strip()
    base_url = (os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).strip()

    if requested in ("", "rule", "none"):
        return RuleBackend(), BackendInfo("rule", "rule", requested,
                                          note="未接模型，走确定性规则解析")

    try:
        if requested in ("openai", "compat", "lmstudio", "ollama"):
            if not model:
                raise RuntimeError(f"未设置 {ENV_MODEL}")
            backend = make_backend("openai", model=model, base_url=base_url)
            return backend, BackendInfo("openai", backend.name, requested, model=model,
                                        note=f"接 {base_url}")
        if requested in ("llamacpp", "gguf"):
            if not model:
                raise RuntimeError(f"未设置 {ENV_MODEL}")
            backend = make_backend("llamacpp", model_path=model)
            return backend, BackendInfo("llamacpp", backend.name, requested, model=model)
        raise RuntimeError(f"未知的 {ENV_BACKEND}={requested!r}")
    except Exception as exc:  # noqa: BLE001 - 任何初始化失败都要降级而不是崩掉
        return RuleBackend(), BackendInfo(
            "rule", "rule", requested, model=model,
            note=f"模型后端不可用（{exc}），已降级到规则后端",
        )


def make_session(user_id: str, *, profile: dict | None = None,
                 log: Any = None, weights: list | None = None) -> Session:
    """按外部状态拼一个 Session。

    log 与 weights 有意采用「引用传递」：外部传进来的对象会被就地修改，
    因此对话与表单天然共享同一份记录，不需要任何同步代码。
    """
    session = Session(user_id=user_id)
    session.profile = profile
    if log is not None:
        session.log = log
    if weights is not None:
        session.weights = weights
    return session


class ChatService:
    """按 user_id 缓存 Router，串起理解层与计算层。"""

    def __init__(self, backend: Backend | None = None,
                 engine: Engine | None = None,
                 fallback: Backend | None = None) -> None:
        if backend is None:
            backend, self.info = backend_from_env()
        else:
            self.info = BackendInfo("custom", getattr(backend, "name", "custom"), "custom")
        self.backend = backend
        self.engine = engine or Engine()
        #: 模型解析失败时的兜底，永远是规则后端
        self.fallback = fallback or RuleBackend()
        self._routers: dict[str, Router] = {}
        self._lock = threading.Lock()

    # -- 会话 ---------------------------------------------------------------

    def router_for(self, user_id: str, session: Session) -> Router:
        """取出该用户的 Router，并把 Session 重新绑定到最新状态。

        Session 每次都要换：api 层的 DayLog 是按「用户+日期」取的，
        跨天时对象会变，不换就会把记录写到昨天那份上。
        """
        with self._lock:
            router = self._routers.get(user_id)
            if router is None:
                router = Router(self.backend, engine=self.engine,
                                session=session, fallback=self.fallback)
                self._routers[user_id] = router
                self._trim()
            else:
                router.session = session
            return router

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._routers.pop(user_id, None)

    def _trim(self, limit: int = 2000) -> None:
        """与 api 层同样做容量保护，防止连接器被无限撑大。"""
        overflow = len(self._routers) - limit
        if overflow > 0:
            for key in list(self._routers)[:overflow]:
                self._routers.pop(key, None)

    # -- 主入口 -------------------------------------------------------------

    def handle(self, user_id: str, text: str, session: Session) -> ChatResult:
        return self.router_for(user_id, session).handle(text)

    def as_dict(self) -> dict[str, Any]:
        return {"backend": self.info.as_dict(), "users": len(self._routers),
                "tools": len(self.engine.db) if hasattr(self.engine.db, "__len__") else None}


__all__ = ["BackendInfo", "ChatService", "backend_from_env", "make_session"]
