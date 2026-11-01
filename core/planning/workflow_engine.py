import asyncio
import uuid
import json
import os

from core.planning.plan_generator import PlanGenerator
from core.planning.workflow_graph import WorkflowGraph
from core.planning.workflow_node import WorkflowNode
from core.planning.dependency_resolver import DependencyResolver
from core.planning.retry_manager import RetryManager
from core.planning.continuation_engine import ContinuationEngine

from core.runtime.continuation.checkpoint_manager import (
    CheckpointManager
)

from core.runtime.continuation.models import (
    WorkflowCheckpoint
)


class WorkflowEngine:

    def __init__(
        self,
        event_bus,
        task_queue
    ):

        self.event_bus = event_bus

        self.task_queue = task_queue

        self.active_workflows = {}

        self.dependency_resolver = (
            DependencyResolver()
        )

        self.retry_manager = (
            RetryManager()
        )

        self.continuation_engine = (
            ContinuationEngine()
        )

        self.planner = (
            PlanGenerator()
        )

        self.checkpoints = (
            CheckpointManager()
        )

        self.workflow_state_file = (
            "state/workflows/workflows.json"
        )

        # Bound by kernel after init — speaks task outcome to user
        self.outcome_reporter = None

        self.load_workflows()

    # ======================================
    # LEGACY WORKFLOWS
    # ======================================

    async def create_workflow(
        self,
        goal,
        steps
    ):

        workflow_id = str(
            uuid.uuid4()
        )

        workflow = {
            "id": workflow_id,
            "goal": goal,
            "steps": steps,
            "current_step": 0,
            "status": "running"
        }

        self.active_workflows[
            workflow_id
        ] = workflow

        self.save_workflows()

        asyncio.create_task(
            self.run_workflow(
                workflow_id
            )
        )

        return workflow_id

    async def run_workflow(
        self,
        workflow_id
    ):

        workflow = (
            self.active_workflows[
                workflow_id
            ]
        )

        while (
            workflow["current_step"]
            <
            len(
                workflow["steps"]
            )
        ):

            step = workflow[
                "steps"
            ][
                workflow[
                    "current_step"
                ]
            ]

            await self.task_queue.add_task(
                step
            )

            workflow[
                "current_step"
            ] += 1

            self.save_workflows()

            await asyncio.sleep(
                1
            )

        workflow[
            "status"
        ] = "completed"

        self.save_workflows()

    async def create_goal_workflow(
        self,
        goal
    ):

        steps = (
            self.planner
            .generate(goal)
        )

        return await (
            self.create_workflow(
                goal,
                steps
            )
        )

    # ======================================
    # STORAGE
    # ======================================

    def save_workflows(self):

        os.makedirs(
            "state/workflows",
            exist_ok=True
        )

        serializable = {}

        for (
            k,
            v
        ) in (
            self.active_workflows
            .items()
        ):

            if isinstance(
                v,
                dict
            ):

                serializable[
                    k
                ] = v

        with open(
            self.workflow_state_file,
            "w"
        ) as file:

            json.dump(
                serializable,
                file,
                indent=4
            )

    def load_workflows(self):

        if (
            not os.path.exists(
                self.workflow_state_file
            )
        ):
            return

        try:

            with open(
                self.workflow_state_file,
                "r"
            ) as file:

                self.active_workflows = (
                    json.load(
                        file
                    )
                )

        except:

            self.active_workflows = {}

    async def recover_workflows(
        self
    ):

        for (
            workflow_id,
            workflow
        ) in (
            self.active_workflows
            .items()
        ):

            if (
                isinstance(
                    workflow,
                    dict
                )
                and
                workflow.get(
                    "status"
                )
                ==
                "running"
            ):

                asyncio.create_task(
                    self.run_workflow(
                        workflow_id
                    )
                )

    # ======================================
    # GRAPH WORKFLOWS
    # ======================================

    async def register_workflow_graph(
        self,
        workflow: WorkflowGraph
    ):

        print(
            "[WorkflowEngine] Graph registered"
        )

        await (
            self.execute_workflow_graph(
                workflow
            )
        )

    async def execute_workflow_graph(
        self,
        workflow: WorkflowGraph
    ):

        workflow.status = "RUNNING"

        # Collect per-node results for outcome reporting
        node_results = []

        while not workflow.is_complete():

            executable_nodes = (
                self.dependency_resolver
                .get_executable_nodes(workflow)
            )

            if not executable_nodes:
                await asyncio.sleep(1)
                continue

            for node in executable_nodes:
                result = await self.execute_node(workflow, node)
                node_results.append(result)

        workflow.status = "COMPLETED"

        # ── Speak smart outcome summary ──────────────────────────────
        if self.outcome_reporter and node_results:
            goal = getattr(workflow, "goal", "") or getattr(workflow, "workflow_id", "")
            asyncio.create_task(
                self.outcome_reporter.report(goal, node_results)
            )

    async def execute_node(
        self,
        workflow,
        node
    ) -> dict:
        """Execute a single workflow node. Returns a result dict for outcome reporting."""

        workflow.mark_active(node.node_id)

        node_result = {
            "node_id": node.node_id,
            "action":  node.action,
            "status":  "completed",
            "error":   ""
        }

        try:
            task = {
                "action": node.action,
                **node.parameters
            }

            await self.task_queue.add_task(task)

            workflow.mark_completed(node.node_id)

            checkpoint = WorkflowCheckpoint(
                workflow_id=getattr(
                    workflow, "workflow_id",
                    getattr(workflow, "id", "unknown")
                ),
                current_step=node.node_id,
                state={
                    "status":    workflow.status,
                    "completed": list(workflow.completed_nodes),
                    "active":    list(workflow.active_nodes),
                    "failed":    list(workflow.failed_nodes)
                }
            )
            self.checkpoints.save(checkpoint)

            print(f"[WorkflowEngine] Node completed: {node.node_id}")

        except Exception as error:
            error_msg = str(error)
            print("[WorkflowEngine Error]", error_msg)

            node_result["status"] = "failed"
            node_result["error"]  = error_msg

            retry_result = self.retry_manager.handle_failure(node)
            if retry_result == "FAILED":
                workflow.mark_failed(node.node_id)

        return node_result
    async def recover_checkpoint(
        self,
        checkpoint
    ):

        print(
            "[WorkflowEngine]"
            " recovering"
        )

        # placeholder

        return checkpoint
    
    async def suspend_workflow(
        self,
        workflow,
        event_type
    ):

        workflow.mark_waiting(
            event_type
        )

        await (

            self
            .continuation_engine
            .wait_for_event(

                workflow.workflow_id,

                "workflow",

                event_type
            )

        )

        print(
            "[Workflow]"
            " suspended"
        )
    
    async def wake_workflow(
        self,
        workflow
    ):

        if (
            workflow.status
            !=
            "WAITING"
        ):

            return

        workflow.resume()

        await (
            self.execute_workflow_graph(
                workflow
            )
        )