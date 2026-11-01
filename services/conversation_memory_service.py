class ConversationMemoryService:

    def __init__(self):

        self.history = []

        self.current_topic = None


    def add_turn(
        self,
        user_message,
        aiva_message
    ):

        self.history.append({

            "user": user_message,

            "aiva": aiva_message

        })

        # Keep only recent conversation turns
        if len(self.history) > 50:

            self.history = self.history[-50:]


    def get_history(
        self,
        limit=None
    ):

        if limit is None:

            return self.history.copy()

        return self.history[-limit:]


    def set_current_topic(
        self,
        topic
    ):

        self.current_topic = topic

        print(
            "[ConversationMemoryService] "
            f"Current topic: {topic}"
        )


    def get_current_topic(self):

        return self.current_topic


    def clear(self):

        self.history.clear()

        self.current_topic = None