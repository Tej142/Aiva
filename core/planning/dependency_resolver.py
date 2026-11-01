from core.planning.workflow_graph import WorkflowGraph


class DependencyResolver:

    def __init__(self):

        pass

    def get_executable_nodes(
        self,
        workflow_graph: WorkflowGraph
    ):

        executable_nodes = []

        for node in workflow_graph.nodes.values():

            # ======================================
            # Skip Non-Pending Nodes
            # ======================================

            if node.status != "PENDING":
                continue

            # ======================================
            # Dependency Check
            # ======================================

            dependencies_satisfied = all(
                dependency in workflow_graph.completed_nodes
                for dependency in node.dependencies
            )

            if not dependencies_satisfied:
                continue

            executable_nodes.append(node)

        return executable_nodes

    def get_blocked_nodes(
        self,
        workflow_graph: WorkflowGraph
    ):

        blocked_nodes = []

        for node in workflow_graph.nodes.values():

            if node.status != "PENDING":
                continue

            dependencies_satisfied = all(
                dependency in workflow_graph.completed_nodes
                for dependency in node.dependencies
            )

            if not dependencies_satisfied:

                blocked_nodes.append(node)

        return blocked_nodes

    def detect_parallel_nodes(
        self,
        workflow_graph: WorkflowGraph
    ):

        parallel_nodes = []

        executable_nodes = self.get_executable_nodes(
            workflow_graph
        )

        for node in executable_nodes:

            if len(node.dependencies) == 0:

                parallel_nodes.append(node)

        return parallel_nodes