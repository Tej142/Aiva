from core.agents.base_agent import BaseAgent

from core.planning.workflow_graph import WorkflowGraph
from core.planning.workflow_node import WorkflowNode


class PlannerAgent(BaseAgent):

    def __init__(
        self,
        mistral_service=None,
        recall_service=None,
        reasoning_service=None,
        conversation_memory_service=None,
        context_resolver_service=None,
        task_pattern_service=None,
        **kwargs
    ):

        super().__init__(
            name="planner_agent",
            **kwargs
        )

        self.mistral_service = (
            mistral_service
        )

        self.recall_service = (
            recall_service
        )

        self.reasoning_service = (
            reasoning_service
        )

        self.conversation_memory_service = (
            conversation_memory_service
        )

        self.context_resolver_service = (
            context_resolver_service
        )

        self.task_pattern_service = (
            task_pattern_service
        )

        self.capabilities = [
            "planning",
            "workflow_generation"
        ]

        self.subscriptions = [
            "goal.created",
            "workflow.failed",
            "runtime.recovery_required"
        ]

    async def initialize(self):

        self.state["status"] = "ready"

        print(
            "[PlannerAgent] Initialized"
        )

    async def handle_event(
        self,
        event
    ):

        event_type = event.get(
            "type"
        )

        print(
            f"[PlannerAgent] Event: {event_type}"
        )

        # =====================================
        # GOAL CREATED
        # =====================================

        if event_type == "goal.created":

            data = event.get(
                "data",
                {}
            )

            goal = data.get(
                "goal"
            )

            if goal:

                await self.plan_goal(
                    goal
                )

        # =====================================
        # RUNTIME RECOVERY
        # =====================================

        elif event_type == "runtime.recovery_required":

            print(
                "[PlannerAgent] "
                "Recovery planning triggered"
            )

            await self.emit_event(

                "workflow.recovery.started",

                {
                    "source":
                    "planner_agent"
                }

            )

    async def execute(
        self,
        task
    ):

        print(
            f"[PlannerAgent] Task: {task}"
        )

    async def plan_goal(
        self,
        goal
    ):

        print(
            f"[PlannerAgent] Planning goal: {goal}"
        )

        original_goal = goal

        # =====================================
        # CONTEXT RESOLUTION
        # =====================================

        resolved_goal = goal

        if self.context_resolver_service:

            resolved_goal = (

                self.context_resolver_service
                .resolve(
                    goal
                )

            )

        if resolved_goal != original_goal:

            print(
                "[PlannerAgent] "
                f"Context resolved: {resolved_goal}"
            )

        # Check if the goal is an actionable task vs a pure knowledge question
        action_indicators = [
            "open ", "launch ", "create ", "search ", "write ", "play ",
            "set ", "mute ", "take ", "start ", "close ", "save ", "note ",
            "generate ", "make ", "run ", "build ", "cheyyi"
        ]
        is_action_goal = any(verb in resolved_goal.lower() for verb in action_indicators) or any(
            sep in resolved_goal.lower() for sep in [" and ", " then ", " also ", " after "]
        )

        # =====================================
        # LONG-TERM MEMORY RECALL (For Questions)
        # =====================================
        if not is_action_goal and self.recall_service:
            memory = self.recall_service.recall(resolved_goal)

            if memory:
                print("[PlannerAgent] Knowledge question memory hit")

                # =================================
                # MEMORY REASONING
                # =================================
                if self.reasoning_service:
                    answer = await self.reasoning_service.answer_from_memory(
                        original_goal,
                        memory["memory"]
                    )

                    print("\n==============================")
                    print("[AIVA MEMORY ANSWER]")
                    print(answer)
                    print("==============================\n")

                    # =============================
                    # UPDATE CONVERSATION MEMORY
                    # =============================
                    if self.conversation_memory_service:
                        entity = memory.get("entity")
                        if entity:
                            self.conversation_memory_service.set_current_topic(entity)
                        self.conversation_memory_service.add_turn(
                            original_goal,
                            answer
                        )

                    return

        # =====================================
        # LEARNED TASK PATTERN RECALL (<50ms)
        # =====================================
        if self.task_pattern_service:
            pattern_match = self.task_pattern_service.match_pattern(resolved_goal)
            if pattern_match and pattern_match.get("workflow"):
                pattern_id = pattern_match.get("pattern_id")
                print(f"\n[PlannerAgent] ⚡ Instant execution from learned pattern: '{pattern_id}'\n")

                workflow = WorkflowGraph(
                    workflow_id=f"workflow_{resolved_goal}",
                    goal=resolved_goal
                )

                for item in pattern_match["workflow"]:
                    node = WorkflowNode(
                        node_id=item["node_id"],
                        action=item["action"],
                        parameters=item.get("parameters", {})
                    )
                    workflow.add_node(node)

                for item in pattern_match["workflow"]:
                    current_node = item["node_id"]
                    for dependency in item.get("dependencies", []):
                        workflow.connect_nodes(dependency, current_node)

                await self.emit_event(
                    "workflow.generated",
                    {
                        "goal": resolved_goal,
                        "original_goal": original_goal,
                        "workflow": workflow,
                        "raw_workflow_data": pattern_match,
                        "source": "learned_pattern"
                    }
                )
                return

        # =====================================
        # MISTRAL WORKFLOW PLANNING
        # =====================================
        if not self.mistral_service:
            print(
                "[PlannerAgent] "
                "Mistral service missing"
            )
            return

        try:
            workflow_data = await (
                self.mistral_service
                .generate_workflow(
                    resolved_goal
                )
            )

            print(
                "\n=== WORKFLOW DATA ==="
            )
            print(
                workflow_data
            )
            print(
                "=====================\n"
            )

            workflow = WorkflowGraph(
                workflow_id=(
                    f"workflow_{resolved_goal}"
                ),
                goal=resolved_goal
            )

            # =====================================
            # CREATE WORKFLOW NODES
            # =====================================
            for item in workflow_data.get(
                "workflow",
                []
            ):
                node = WorkflowNode(
                    node_id=item[
                        "node_id"
                    ],
                    action=item[
                        "action"
                    ],
                    parameters=item.get(
                        "parameters",
                        {}
                    )
                )
                workflow.add_node(
                    node
                )

            # =====================================
            # CONNECT DEPENDENCIES
            # =====================================
            for item in workflow_data.get(
                "workflow",
                []
            ):
                current_node = item[
                    "node_id"
                ]
                for dependency in item.get(
                    "dependencies",
                    []
                ):
                    workflow.connect_nodes(
                        dependency,
                        current_node
                    )

            print(
                "[PlannerAgent] "
                "Workflow generated by Mistral"
            )

            # =====================================
            # EMIT WORKFLOW
            # =====================================
            await self.emit_event(
                "workflow.generated",
                {
                    "goal":
                    resolved_goal,
                    "original_goal":
                    original_goal,
                    "workflow":
                    workflow,
                    "raw_workflow_data":
                    workflow_data,
                    "source":
                    "mistral"
                }
            )

        except Exception as error:
            print(
                "[PlannerAgent Error]",
                error
            )