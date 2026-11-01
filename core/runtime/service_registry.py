class ServiceRegistry:

    def __init__(self):

        self.services = {}

    def register(self, name, service):

        self.services[name] = service

        print(f"[AIVA] Registered service: {name}")

    def get(self, name):

        return self.services.get(name)

    def remove(self, name):

        if name in self.services:
            del self.services[name]

    def all(self):

        return self.services