class ActionDispatcher:

    def __init__(
        self,
        capability_registry
    ):

        self.capabilities = capability_registry

    async def dispatch(self, action):

        action_type = action.get("action")

        handler = self.capabilities.get(
            action_type
        )

        if not handler:

            print(
                f"[AIVA] Unknown action: {action_type}"
            )

            return {
                "status": "unknown_action"
            }

        return await handler(action)