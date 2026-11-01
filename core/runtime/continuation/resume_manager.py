class ResumeManager:

    def __init__(
        self,
        checkpoint,
        workflow_engine
    ):

        self.cp = checkpoint

        self.engine = (
            workflow_engine
        )

    async def resume(
        self,
        workflow_id
    ):

        state = (
            self.cp
            .load(
                workflow_id
            )
        )

        if (
            not state
        ):

            return

        await (
            self.engine
            .recover_checkpoint(
                state
            )
        )