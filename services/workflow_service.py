"""
WorkflowService — High-level facade over WorkflowEngine.

Provides a clean async API for:
  - Creating and running new workflows from a goal string
  - Listing, pausing, resuming, and cancelling active workflows
  - Querying workflow status for the Web HUD

Decouples external callers (aiva_server.py, plugins) from the
internal WorkflowEngine / PlannerAgent wiring in the kernel.
"""

import asyncio
from core.shared.logger import get_logger

logger = get_logger("WorkflowService")


class WorkflowService:
    """
    Facade service that wraps the kernel's WorkflowEngine.
    Must be initialized with the kernel instance after the kernel starts.
    """

    def __init__(self, kernel=None):
        self.kernel = kernel
        logger.info("WorkflowService initialized")

    def bind(self, kernel):
        """Late-bind the kernel after startup (called from kernel.initialize_runtime)."""
        self.kernel = kernel

    # ------------------------------------------------------------------
    # CREATE & RUN
    # ------------------------------------------------------------------

    async def run_goal(self, goal: str) -> dict:
        """
        Generate and execute a workflow DAG for the given goal string.
        Delegates to PlannerAgent → WorkflowEngine.

        Returns:
            {
              "status":      "started" | "error",
              "workflow_id": str,
              "node_count":  int,
              "message":     str
            }
        """
        if not self.kernel:
            return {"status": "error", "message": "Kernel not bound to WorkflowService"}

        try:
            planner = self.kernel.planner_agent
            workflow_data = await planner.plan(goal)

            workflow_id = workflow_data.get("workflow_id", "unknown")
            nodes = workflow_data.get("workflow", [])

            logger.info(f"WorkflowService: started workflow '{workflow_id}' with {len(nodes)} nodes for goal: {goal}")

            return {
                "status": "started",
                "workflow_id": workflow_id,
                "node_count": len(nodes),
                "message": f"Workflow started with {len(nodes)} steps."
            }
        except Exception as e:
            logger.error(f"WorkflowService.run_goal error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # INTROSPECTION
    # ------------------------------------------------------------------

    def list_active(self) -> list:
        """
        Returns a list of currently active workflow summaries.
        Each entry: {"workflow_id": str, "goal": str, "node_count": int}
        """
        if not self.kernel:
            return []
        try:
            engine = self.kernel.workflow_engine
            summaries = []
            for wf_id, wf in engine.active_workflows.items():
                summaries.append({
                    "workflow_id": wf_id,
                    "goal": getattr(wf, "goal", ""),
                    "node_count": len(getattr(wf, "nodes", {})),
                })
            return summaries
        except Exception as e:
            logger.warning(f"WorkflowService.list_active error: {e}")
            return []

    def get_status(self, workflow_id: str) -> dict:
        """Returns the status dict for a specific workflow by ID."""
        if not self.kernel:
            return {"status": "error", "message": "Kernel not bound"}
        try:
            engine = self.kernel.workflow_engine
            wf = engine.active_workflows.get(workflow_id)
            if not wf:
                return {"status": "not_found", "workflow_id": workflow_id}
            return {
                "status": "active",
                "workflow_id": workflow_id,
                "goal": getattr(wf, "goal", ""),
                "nodes": [
                    {
                        "node_id": nid,
                        "status": getattr(node, "status", "unknown"),
                        "action": getattr(node, "action", ""),
                    }
                    for nid, node in getattr(wf, "nodes", {}).items()
                ]
            }
        except Exception as e:
            logger.warning(f"WorkflowService.get_status error: {e}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # CONTROL
    # ------------------------------------------------------------------

    async def cancel(self, workflow_id: str) -> dict:
        """Cancel a running workflow by ID."""
        if not self.kernel:
            return {"status": "error", "message": "Kernel not bound"}
        try:
            engine = self.kernel.workflow_engine
            if workflow_id not in engine.active_workflows:
                return {"status": "not_found", "workflow_id": workflow_id}
            del engine.active_workflows[workflow_id]
            logger.info(f"Workflow '{workflow_id}' cancelled.")
            return {"status": "cancelled", "workflow_id": workflow_id}
        except Exception as e:
            logger.warning(f"WorkflowService.cancel error: {e}")
            return {"status": "error", "message": str(e)}
