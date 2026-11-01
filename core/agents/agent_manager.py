class AgentManager:

    def __init__(self):
        self.agents = {}

    def register(self, agent):
        self.agents[agent.name] = agent

    async def initialize_all(self):

        for agent in self.agents.values():
            await agent.initialize()

    async def shutdown_all(self):

        for agent in self.agents.values():
            await agent.shutdown()

    async def dispatch_event(self, event):

        event_type = event.get("type")

        for agent in self.agents.values():

            if (
                "*" in agent.subscriptions
                or event_type in agent.subscriptions
            ):
                await agent.handle_event(event)

    def get_agent(self, name):

        return self.agents.get(name)