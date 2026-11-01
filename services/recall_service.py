import json
import os


class RecallService:

    def __init__(self):

        self.memory_file = (
            "memory/knowledge_memory.json"
        )

    def recall(
        self,
        query
    ):

        if not os.path.exists(
            self.memory_file
        ):
            return None

        with open(
            self.memory_file,
            "r"
        ) as file:

            knowledge = json.load(
                file
            )

        query = (
            query
            .lower()
            .strip()
        )

        for entity, data in knowledge.items():

            if entity.lower() in query:

                return {

                    "entity": entity,

                    "memory": data

                }

        return None