class CapabilityRegistry:

    def __init__(self):

        self.capabilities = {}

    def register(self, action_name, handler):

        self.capabilities[action_name] = handler

        print(f"[AIVA] Registered capability: {action_name}")

    def get(self, action_name):

        return self.capabilities.get(action_name)

    def all(self):

        return self.capabilities