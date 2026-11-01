from typing import Dict, List

from core.planning.workflow_node import WorkflowNode


class WorkflowGraph:

    def __init__(self, workflow_id: str, goal: str = ""):

        self.workflow_id = workflow_id
        self.goal = goal  # Original user goal — used by OutcomeReporter

        # ======================================
        # Graph Nodes
        # ======================================

        self.nodes: Dict[str, WorkflowNode] = {}

        # ======================================
        # Runtime State
        # ======================================

        self.completed_nodes: List[str] = []

        self.failed_nodes: List[str] = []

        self.active_nodes: List[str] = []

        self.status = "PENDING"

        self.wait_reason = None

    def add_node(self, node: WorkflowNode):

        self.nodes[node.node_id] = node

    def connect_nodes(
        self,
        from_node: str,
        to_node: str
    ):

        if from_node not in self.nodes:
            return

        if to_node not in self.nodes:
            return

        self.nodes[from_node].add_next_node(
            to_node
        )

        self.nodes[to_node].add_dependency(
            from_node
        )

    def get_ready_nodes(self):

        ready_nodes = []

        for node in self.nodes.values():

            if node.status != "PENDING":
                continue

            if node.can_execute(
                self.completed_nodes
            ):
                ready_nodes.append(node)

        return ready_nodes

    def mark_completed(self, node_id: str):

        if node_id not in self.completed_nodes:

            self.completed_nodes.append(node_id)

        if node_id in self.active_nodes:

            self.active_nodes.remove(node_id)

        node = self.nodes.get(node_id)

        if node:
            node.status = "COMPLETED"

    def mark_failed(self, node_id: str):

        if node_id not in self.failed_nodes:

            self.failed_nodes.append(node_id)

        if node_id in self.active_nodes:

            self.active_nodes.remove(node_id)

        node = self.nodes.get(node_id)

        if node:
            node.status = "FAILED"

    def mark_active(self, node_id: str):

        if node_id not in self.active_nodes:

            self.active_nodes.append(node_id)

        node = self.nodes.get(node_id)

        if node:
            node.status = "RUNNING"

    def mark_waiting(
        self,
        reason
    ):

        self.status = (
            "WAITING"
        )

        self.wait_reason = (
            reason
        )


    def resume(self):

        self.status = (
            "RUNNING"
        )

        self.wait_reason = (
            None
        )

    def is_complete(self):

        return len(self.completed_nodes) == len(
            self.nodes
        )

    def to_dict(self):

        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "completed_nodes": self.completed_nodes,
            "failed_nodes": self.failed_nodes,
            "active_nodes": self.active_nodes,
            "nodes": {
                node_id: node.to_dict()
                for node_id, node in self.nodes.items()
            }
        }