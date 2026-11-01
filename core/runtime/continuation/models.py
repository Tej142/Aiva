from dataclasses import dataclass
from typing import Optional
import time


@dataclass
class WorkflowCheckpoint:

    workflow_id: str

    current_step: str

    state: dict

    waiting: bool = False

    wait_reason: Optional[
        str
    ] = None

    completed: bool = False

    timestamp: float = (
        time.time()
    )