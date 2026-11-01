"""
ExecutionContext — Scoped execution state passed to every capability handler.

Each workflow node execution receives an ExecutionContext that carries:
  - workflow_id / node_id: traceability for logs and checkpoints
  - kernel: reference to the live AIVAKernel (services, voice, memory)
  - results: accumulated outputs from previously completed nodes
  - metadata: arbitrary key-value bag for handler-specific state

Handlers can read context.results to access outputs of dependency nodes,
and write to context.results so downstream nodes can consume them.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from core.shared.logger import get_logger

logger = get_logger("ExecutionContext")


@dataclass
class ExecutionContext:
    """
    Scoped context object for a single workflow node execution.

    Attributes:
        workflow_id:  Unique identifier of the parent workflow.
        node_id:      Unique identifier of the current node being executed.
        goal:         The original user goal string that spawned this workflow.
        kernel:       Live reference to AIVAKernel (for service access).
        results:      Dict mapping node_id → output from previously completed nodes.
        metadata:     Arbitrary bag for handler-specific data (e.g., screenshot path).
    """

    workflow_id: str = ""
    node_id: str = ""
    goal: str = ""
    kernel: Optional[Any] = None
    results: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # CONVENIENCE ACCESSORS
    # ------------------------------------------------------------------

    def get_result(self, node_id: str, default=None) -> Any:
        """Return the output of a previously completed dependency node."""
        return self.results.get(node_id, default)

    def set_result(self, node_id: str, value: Any):
        """Store the output of the current (or any) node for downstream use."""
        self.results[node_id] = value
        logger.debug(f"[{self.workflow_id}/{node_id}] result stored")

    def set_meta(self, key: str, value: Any):
        """Store handler-specific metadata (e.g., 'screenshot_path', 'focused_window')."""
        self.metadata[key] = value

    def get_meta(self, key: str, default=None) -> Any:
        return self.metadata.get(key, default)

    # ------------------------------------------------------------------
    # SERVICE SHORTCUTS (via kernel)
    # ------------------------------------------------------------------

    @property
    def voice_service(self):
        return getattr(self.kernel, "voice_service", None)

    @property
    def memory_service(self):
        return getattr(self.kernel, "memory_service", None)

    @property
    def user_memory_service(self):
        return getattr(self.kernel, "user_memory_service", None)

    @property
    def conversation_memory_service(self):
        return getattr(self.kernel, "conversation_memory_service", None)

    @property
    def browser_service(self):
        return getattr(self.kernel, "browser_service", None)

    def __repr__(self):
        return (
            f"ExecutionContext(workflow={self.workflow_id!r}, "
            f"node={self.node_id!r}, results={list(self.results.keys())})"
        )
