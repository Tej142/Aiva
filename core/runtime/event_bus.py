class EventBus:

    def __init__(self):

        self.listeners = {}

    def on(
        self,
        event_name,
        callback
    ):

        if event_name not in self.listeners:

            self.listeners[
                event_name
            ] = []

        self.listeners[
            event_name
        ].append(
            callback
        )

    async def emit(
        self,
        event,
        data=None
    ):

        # ==========================
        # Support Dict Events
        # ==========================

        if isinstance(
            event,
            dict
        ):

            event_name = (
                event.get(
                    "type"
                )
            )

            data = (
                event.get(
                    "data",
                    {}
                )
            )

        else:

            event_name = event

        listeners = []

        if "*" in self.listeners:

            listeners.extend(
                self.listeners["*"]
            )

        if event_name in self.listeners:

            listeners.extend(
                self.listeners[
                    event_name
                ]
            )

        for callback in listeners:

            if isinstance(event, dict):

                await callback(
                    event
                )

            else:

                await callback(
                    {
                        "type": event_name,
                        "data": data
                    }
                )