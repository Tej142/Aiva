import asyncio
from core.shared.logger import get_logger

logger = get_logger("WorkflowObserver")


class WorkflowObserver:
    def __init__(self, event_bus, task_pattern_service):
        self.event_bus = event_bus
        self.task_pattern_service = task_pattern_service
        self.running = False
        self.pending_workflows = {}  # workflow_id -> {goal, workflow_data}

    async def start(self):
        """Starts observing workflow lifecycle events on the EventBus."""
        self.running = True
        logger.info("WorkflowObserver started and listening for workflow events.")

        self.event_bus.on("workflow.generated", self.handle_workflow_generated)
        self.event_bus.on("workflow.completed", self.handle_workflow_completed)
        self.event_bus.on("goal.completed", self.handle_goal_completed)

    async def handle_workflow_generated(self, event):
        """Caches generated workflow for pattern evaluation upon completion."""
        data = event.get("data", {})
        goal = data.get("goal") or data.get("original_goal") or ""
        workflow_obj = data.get("workflow")
        raw_workflow_data = data.get("raw_workflow_data")

        if not goal or not workflow_obj:
            return

        workflow_id = getattr(workflow_obj, "workflow_id", getattr(workflow_obj, "id", str(id(workflow_obj))))

        # Extract serializable node list
        nodes_list = []
        if raw_workflow_data and isinstance(raw_workflow_data, dict) and "workflow" in raw_workflow_data:
            nodes_list = raw_workflow_data["workflow"]
        elif hasattr(workflow_obj, "nodes"):
            for nid, n in workflow_obj.nodes.items():
                nodes_list.append({
                    "node_id": n.node_id,
                    "action": n.action,
                    "parameters": n.parameters,
                    "dependencies": list(n.dependencies)
                })

        if nodes_list:
            self.pending_workflows[workflow_id] = {
                "goal": goal,
                "workflow_data": {"workflow": nodes_list}
            }
            # Also proactively learn / register pattern
            try:
                pid = self.task_pattern_service.learn_pattern(goal, {"workflow": nodes_list})
                if pid:
                    logger.info(f"[WorkflowObserver] Learned pattern '{pid}' for goal '{goal}'")
            except Exception as e:
                logger.warning(f"Failed to learn pattern: {e}")

    async def handle_workflow_completed(self, event):
        """Reinforces learned pattern upon verified successful completion."""
        data = event.get("data", {})
        workflow_id = data.get("workflow_id")

        if workflow_id and workflow_id in self.pending_workflows:
            item = self.pending_workflows.pop(workflow_id)
            goal = item["goal"]
            wdata = item["workflow_data"]
            try:
                pid = self.task_pattern_service.learn_pattern(goal, wdata)
                logger.info(f"[WorkflowObserver] Confirmed successful execution for pattern '{pid}'")
            except Exception as e:
                logger.warning(f"Error reinforcing pattern: {e}")

    async def handle_goal_completed(self, event):
        pass

    async def stop(self):
        self.running = False
