import json
from pathlib import Path


class CognitionStore:

    def __init__(self):

        self.path = Path(
            "memory/runtime"
        )

        self.path.mkdir(
            parents=True,
            exist_ok=True
        )

    def persist(
        self,
        workflow_id,
        cognition
    ):

        (
            self.path
            /
            f"{workflow_id}.json"
        ).write_text(

            json.dumps(
                cognition,
                indent=2
            )
        )