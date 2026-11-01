import os
import sys
import time
import json
import uuid
import asyncio
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from services.database_service import DatabaseService
from services.device_manager import DeviceManager
from services.device_channel import DeviceChannel


class MockWebSocket:
    """Mock WebSocket for end-to-end testing of DeviceChannel without running an HTTP server."""
    def __init__(self):
        self.sent_messages = []
        self.inbox = asyncio.Queue()
        self.closed = False
        self.reason = None

    def send(self, data):
        if self.closed:
            raise RuntimeError("Cannot send on closed socket")
        self.sent_messages.append(json.loads(data) if isinstance(data, str) else data)

    def receive(self, timeout=None):
        # We can implement non-blocking or queue-based receive for tests
        return None

    def close(self, reason=None):
        self.closed = True
        self.reason = reason


class TestWebSocketE2E(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseService(":memory:")
        self.device_manager = DeviceManager(self.db)
        self.device_channel = DeviceChannel(self.db, self.device_manager)

    def test_mock_device_lifecycle_and_command_dispatch(self):
        async def run_test():
            user = self.db.create_user(str(uuid.uuid4()), "e2e_user@example.com")
            user_id = user["id"]

            # Register device
            ok, msg, res = self.device_manager.register_device(
                user_id=user_id,
                name="Work Laptop",
                platform="windows",
                app_version="1.0.0"
            )
            self.assertTrue(ok)
            device_id = res["device"]["id"]
            raw_token = res["device_token"]

            mock_ws = MockWebSocket()

            # Emulate connection handshake in channel
            with self.device_channel._channel_lock:
                self.device_channel._connections[device_id] = mock_ws
                self.device_channel._device_user_map[device_id] = user_id

            self.device_manager.update_heartbeat(device_id, "online")
            self.assertTrue(self.device_channel.is_device_online(device_id))

            # Set loop
            loop = asyncio.get_running_loop()
            self.device_channel.set_event_loop(loop)

            # Background task simulating device responding to command
            async def simulated_device_responder():
                while not mock_ws.sent_messages:
                    await asyncio.sleep(0.01)

                sent_cmd = mock_ws.sent_messages[-1]
                self.assertEqual(sent_cmd["type"], "command")
                self.assertEqual(sent_cmd["action"], "open_app")
                cmd_id = sent_cmd["id"]

                # Simulate device sending result back
                # Complete the pending future directly via channel's internal pending_commands
                fut = self.device_channel._pending_commands.get(cmd_id)
                if fut:
                    fut.set_result({
                        "status": "success",
                        "output": {"message": "Notepad opened successfully."}
                    })

            responder_task = asyncio.create_task(simulated_device_responder())

            # Dispatch command from cloud
            dispatch_res = await self.device_channel.dispatch_command(
                user_id=user_id,
                action="open_app",
                params={"app_name": "notepad"},
                timeout=5.0
            )

            await responder_task

            self.assertEqual(dispatch_res["status"], "success")
            self.assertIn("Notepad opened", dispatch_res["result"])

            # Verify audit log in DB
            logs = self.db.get_recent_audit_logs(user_id)
            self.assertGreaterEqual(len(logs), 1)
            self.assertEqual(logs[0]["action"], "open_app")
            self.assertEqual(logs[0]["device_id"], device_id)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()

