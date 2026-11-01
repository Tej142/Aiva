from typing import Dict, List, Optional


class WorkflowNode:

    def __init__(
        self,
        node_id: str,
        action: str,
        parameters: Optional[Dict] = None
    ):

        self.node_id = node_id

        self.action = action

        self.parameters = parameters or {}

        # ======================================
        # Graph Relationships
        # ======================================

        self.dependencies: List[str] = []

        self.next_nodes: List[str] = []

        # ======================================
        # Runtime State
        # ======================================

        self.status = "PENDING"

        self.retry_count = 0

        self.max_retries = 3

        # ======================================
        # Continuation Rules
        # ======================================

        self.wait_for_event = None

        self.timeout = None

        # ======================================
        # Conditions
        # ======================================

        self.conditions = []

    def add_dependency(self, node_id: str):

        self.dependencies.append(node_id)

    def add_next_node(self, node_id: str):

        self.next_nodes.append(node_id)

    def set_wait_condition(
        self,
        event_type: str,
        timeout: int = None
    ):

        self.wait_for_event = event_type

        self.timeout = timeout

    def can_execute(
        self,
        completed_nodes: List[str]
    ) -> bool:

        return all(
            dependency in completed_nodes
            for dependency in self.dependencies
        )

    def to_dict(self):

        return {
            "node_id": self.node_id,
            "action": self.action,
            "parameters": self.parameters,
            "dependencies": self.dependencies,
            "next_nodes": self.next_nodes,
            "status": self.status,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "wait_for_event": self.wait_for_event,
            "timeout": self.timeout,
            "conditions": self.conditions
        }