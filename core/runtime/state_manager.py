import json
import os


class StateManager:

    def __init__(self):

        self.state = {}

        self.state_file = "state/runtime/runtime_state.json"

        self.load()

    def set(self, key, value):

        self.state[key] = value

        self.save()

    def get(self, key, default=None):

        return self.state.get(key, default)

    def remove(self, key):

        if key in self.state:

            del self.state[key]

            self.save()

    def all(self):

        return self.state

    def save(self):

        os.makedirs(
            "state/runtime",
            exist_ok=True
        )

        with open(self.state_file, "w") as file:

            json.dump(
                self.state,
                file,
                indent=4
            )

    def load(self):

        if os.path.exists(self.state_file):

            with open(self.state_file, "r") as file:

                self.state = json.load(file)