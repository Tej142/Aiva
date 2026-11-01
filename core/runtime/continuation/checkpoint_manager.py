import json
from pathlib import Path


class CheckpointManager:

    def __init__(self):

        self.root = Path("state/checkpoints")

        self.root.mkdir(
            parents=True,
            exist_ok=True
        )

    def save(self, checkpoint):

        file = (
            self.root
            /
            f"{checkpoint.workflow_id}.json"
        )

        file.write_text(
            json.dumps(
                checkpoint.__dict__,
                indent=2
            )
        )

    def load(self, workflow_id):

        file = (
            self.root
            /
            f"{workflow_id}.json"
        )

        if not file.exists():

            return None

        return json.loads(
            file.read_text()
        )