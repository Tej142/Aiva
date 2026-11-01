import asyncio
import json
from pathlib import Path
from typing import Dict


class ContinuationEngine:

    def __init__(self):

        self.waiting_nodes = {}

        self.state_path = (
            Path(
                "state/continuation"
            )
        )

        self.state_path.mkdir(
            parents=True,
            exist_ok=True
        )

    async def wait_for_event(
        self,
        workflow_id,
        node_id,
        event_type,
        timeout=None
    ):

        state = {

            "workflow_id":
            workflow_id,

            "node_id":
            node_id,

            "event_type":
            event_type,

            "timeout":
            timeout,

            "status":
            "WAITING"
        }

        self.waiting_nodes[
            node_id
        ] = state

        self.save()

        if timeout:

            asyncio.create_task(
                self.handle_timeout(
                    node_id,
                    timeout
                )
            )

    async def handle_timeout(
        self,
        node_id,
        timeout
    ):

        await asyncio.sleep(
            timeout
        )

        if (
            node_id
            not in self.waiting_nodes
        ):
            return

        node = (
            self.waiting_nodes[
                node_id
            ]
        )

        if (
            node["status"]
            ==
            "WAITING"
        ):

            node[
                "status"
            ] = "TIMEOUT"

            self.save()

    async def resolve_event(
        self,
        event_type
    ):

        resolved = []

        for (
            node_id,
            node
        ) in list(
            self.waiting_nodes
            .items()
        ):

            if (
                node[
                    "event_type"
                ]
                !=
                event_type
            ):
                continue

            node[
                "status"
            ] = "RESOLVED"

            resolved.append(
                node
            )

        self.save()

        return resolved

    def save(self):

        file = (
            self.state_path
            /
            "waiting.json"
        )

        file.write_text(

            json.dumps(
                self.waiting_nodes,
                indent=2
            )

        )

    def restore(self):

        file = (
            self.state_path
            /
            "waiting.json"
        )

        if (
            not file.exists()
        ):
            return

        self.waiting_nodes = (
            json.loads(
                file.read_text()
            )
        )

    def get_waiting_nodes(
        self
    ):

        return (
            self.waiting_nodes
        )