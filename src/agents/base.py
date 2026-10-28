"""Agent base class.

An agent = a prompt file + a JSON contract + a typed result. The prompt text
lives in ``prompts/<name>.md`` so it can be edited without touching Python.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from src.agents.llm import LLMClient, LLMRequest
from src.core.config import AppConfig
from src.core.errors import AgentError, LLMError
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

_FALLBACK_PROMPT = (
    "You are a careful technical analyst working on AI and blockchain news. "
    "Follow the instructions and respond with a single valid JSON object."
)


class Agent(ABC):
    """Shared plumbing: prompt loading, LLM invocation, config overrides."""

    name: ClassVar[str] = "agent"
    task: ClassVar[str] = "agent"
    prompt_file: ClassVar[str] = ""

    def __init__(
        self,
        *,
        config: AppConfig,
        llm: LLMClient,
        prompt_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.prompt_dir = prompt_dir or config.paths.prompts
        self.log = get_logger(f"{__name__}.{self.name}")
        self._prompt: str | None = None

    # ---------------------------------------------------------------- prompting
    @property
    def system_prompt(self) -> str:
        """Contents of ``prompts/<prompt_file>``, cached."""
        if self._prompt is not None:
            return self._prompt
        path = self.prompt_dir / (self.prompt_file or f"{self.name}.md")
        try:
            self._prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.log.warning("Prompt file unavailable (%s); using the built-in fallback", exc)
            self._prompt = _FALLBACK_PROMPT
        return self._prompt

    def setting(self, key: str, default: Any = None) -> Any:
        """Agent-scoped setting with a global fallback (``agents.<name>.<key>``)."""
        specific = self.config.get(f"agents.{self.name}.{key}")
        if specific is not None:
            return specific
        shared = self.config.get(f"agents.{key}")
        return shared if shared is not None else default

    # -------------------------------------------------------------------- calls
    def ask_json(self, payload: dict[str, Any], *, instruction: str = "") -> dict[str, Any]:
        """Send ``payload`` to the LLM and return the parsed JSON object."""
        user = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if instruction:
            user = f"{instruction}\n\nINPUT:\n{user}"
        request = LLMRequest(
            task=self.task,
            system=self.system_prompt,
            user=user,
            payload=payload,
            temperature=float(self.setting("temperature", 0.3)),
            max_tokens=int(self.setting("max_tokens", 2000)),
            json_mode=True,
        )
        retries = int(self.config.get("agents.json_retries", 2))
        try:
            return self.llm.complete_json(request, retries=retries)
        except LLMError as exc:
            raise AgentError(str(exc), agent=self.name) from exc

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the agent."""
