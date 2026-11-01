import json
import os


class SessionService:

    def __init__(self):

        self.session_file = (
            "state/sessions/session.json"
        )

        self.session = self.load_session()

    def set(self, key, value):

        self.session[key] = value

        self.save()

    def get(self, key, default=None):

        return self.session.get(key, default)

    def save(self):

        os.makedirs(
            "state/sessions",
            exist_ok=True
        )

        with open(self.session_file, "w") as file:

            json.dump(
                self.session,
                file,
                indent=4
            )

    def load_session(self):

        if os.path.exists(self.session_file):

            with open(self.session_file, "r") as file:

                return json.load(file)

        return {}