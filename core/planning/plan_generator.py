class PlanGenerator:

    def generate(self, goal):

        goal = goal.lower()

        if "google" in goal:

            return [
                {
                    "action": "open_url",
                    "url": "https://google.com"
                }
            ]

        if "youtube" in goal:

            return [
                {
                    "action": "open_url",
                    "url": "https://youtube.com"
                }
            ]

        return []