from core.planning.workflow_node import WorkflowNode


class RetryManager:

    def __init__(self):

        pass

    def can_retry(
        self,
        node: WorkflowNode
    ) -> bool:

        return (
            node.retry_count < node.max_retries
        )

    def increment_retry(
        self,
        node: WorkflowNode
    ):

        node.retry_count += 1

    def reset_retry(
        self,
        node: WorkflowNode
    ):

        node.retry_count = 0

    def mark_for_retry(
        self,
        node: WorkflowNode
    ):

        node.status = "PENDING"

    def handle_failure(
        self,
        node: WorkflowNode
    ):

        # ======================================
        # Retry Available
        # ======================================

        if self.can_retry(node):

            self.increment_retry(node)

            self.mark_for_retry(node)

            print(
                f"[RetryManager] Retrying node: "
                f"{node.node_id} "
                f"({node.retry_count}/"
                f"{node.max_retries})"
            )

            return "RETRY"

        # ======================================
        # Permanent Failure
        # ======================================

        node.status = "FAILED"

        print(
            f"[RetryManager] Node permanently failed: "
            f"{node.node_id}"
        )

        return "FAILED"