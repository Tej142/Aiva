class WaitStateManager:

    def __init__(self):

        self.waiting = {}

    def enter(
        self,
        workflow_id,
        reason
    ):

        self.waiting[
            workflow_id
        ] = reason

    def exit(
        self,
        workflow_id
    ):

        self.waiting.pop(
            workflow_id,
            None
        )

    def is_waiting(
        self,
        workflow_id
    ):

        return (
            workflow_id
            in self.waiting
        )