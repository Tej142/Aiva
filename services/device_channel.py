import asyncio
import json
import time
import uuid
import threading
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import parse_qs, urlparse

from services.database_service import DatabaseService
from services.device_manager import DeviceManager
from core.shared.logger import get_logger

logger = get_logger("DeviceChannel")


class DeviceChannel:
    """
    Manages outbound WebSocket connections from AIVA Device Agents.
    Provides routing, dispatching, ack/heartbeat handling, and audit logging.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db: Optional[DatabaseService] = None, device_manager: Optional[DeviceManager] = None):
        with cls._lock:
            if db is not None:
                inst = super(DeviceChannel, cls).__new__(cls)
                inst._init_channel(db, device_manager)
                return inst
            if cls._instance is None:
                cls._instance = super(DeviceChannel, cls).__new__(cls)
                cls._instance._init_channel(None, None)
            return cls._instance

    def _init_channel(self, db: Optional[DatabaseService] = None, device_manager: Optional[DeviceManager] = None):
        self.db = db or DatabaseService()
        self.device_manager = device_manager or DeviceManager(self.db)

        # Mapping: device_id -> active WebSocket object
        self._connections: Dict[str, Any] = {}
        # Mapping: device_id -> user_id
        self._device_user_map: Dict[str, str] = {}
        # Mapping: command_id -> asyncio.Future
        self._pending_commands: Dict[str, asyncio.Future] = {}
        # Connection synchronization lock
        self._channel_lock = threading.Lock()
        # Reference to background event loop for threadsafe future resolution
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def is_device_online(self, device_id: str) -> bool:
        with self._channel_lock:
            return device_id in self._connections

    def get_online_devices_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        user_devices = self.device_manager.list_devices(user_id)
        with self._channel_lock:
            online_ids = set(self._connections.keys())

        return [d for d in user_devices if d["id"] in online_ids]

    def find_target_device(self, user_id: str, target_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Finds the target online device for a user.
        If target_name is supplied, matches by name.
        Otherwise selects the most recently active online device.
        """
        online_devices = self.get_online_devices_for_user(user_id)
        if not online_devices:
            return None

        if target_name:
            clean_target = target_name.strip().lower()
            # Try exact match or substring match
            for dev in online_devices:
                if clean_target in dev["name"].lower():
                    return dev

        # Default: return the device with the most recent last_seen timestamp
        online_devices.sort(key=lambda d: d.get("last_seen") or 0, reverse=True)
        return online_devices[0]

    # -------------------------------------------------------------
    # WEBSOCKET CONNECTION LIFECYCLE
    # -------------------------------------------------------------
    def handle_ws_connection(self, ws, environ: Dict[str, Any]):
        """
        Handles an incoming WebSocket connection (from flask-sock).
        Authenticates via query param '?token=...' or first JSON message.
        """
        token = None

        # 1. Check query string: /ws/device?token=aiva_dev_...
        query_string = environ.get("QUERY_STRING", "")
        if query_string:
            params = parse_qs(query_string)
            if "token" in params and params["token"]:
                token = params["token"][0]

        device_record = None
        if token:
            device_record = self.device_manager.verify_device_token(token)

        # 2. If not authenticated via query string, wait for first auth packet
        if not device_record:
            try:
                raw_init = ws.receive(timeout=10.0)
                if raw_init:
                    init_data = json.loads(raw_init)
                    if init_data.get("type") == "auth":
                        token = init_data.get("token")
                        device_record = self.device_manager.verify_device_token(token)
            except Exception as e:
                logger.warning(f"Handshake error or timeout: {e}")

        if not device_record:
            logger.warning("Device connection rejected: Invalid or missing device token.")
            try:
                ws.send(json.dumps({
                    "v": 1,
                    "type": "error",
                    "code": "unauthorized",
                    "message": "Invalid or missing device token."
                }))
                ws.close(reason="Unauthorized")
            except Exception:
                pass
            return

        device_id = device_record["id"]
        user_id = device_record["user_id"]
        device_name = device_record["name"]

        # Register connection
        with self._channel_lock:
            # If an existing socket was connected for this device, gracefully close it
            old_ws = self._connections.get(device_id)
            if old_ws and old_ws != ws:
                try:
                    old_ws.close()
                except Exception:
                    pass

            self._connections[device_id] = ws
            self._device_user_map[device_id] = user_id

        self.device_manager.update_heartbeat(device_id, status="online")
        logger.info(f"Device connected: '{device_name}' ({device_id}) for user {user_id}")

        # Send connection confirmation
        try:
            ws.send(json.dumps({
                "v": 1,
                "type": "connected",
                "device_id": device_id,
                "device_name": device_name,
                "server_time": time.time()
            }))
        except Exception as e:
            logger.error(f"Failed to send connected handshake: {e}")
            return

        # Main message loop
        try:
            while True:
                raw_msg = ws.receive()
                if raw_msg is None:
                    break

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                msg_type = msg.get("type")

                # Heartbeat
                if msg_type == "heartbeat":
                    self.device_manager.update_heartbeat(device_id, status="online")
                    ws.send(json.dumps({
                        "v": 1,
                        "type": "heartbeat_ack",
                        "timestamp": time.time()
                    }))

                # Command Ack
                elif msg_type == "ack":
                    cmd_id = msg.get("id")
                    if cmd_id:
                        self.db.update_command_log(cmd_id, status="acknowledged")

                # Command Result
                elif msg_type == "result":
                    cmd_id = msg.get("id")
                    status = msg.get("status", "success")
                    output = msg.get("output", {})
                    output_str = json.dumps(output) if isinstance(output, (dict, list)) else str(output)

                    self.db.update_command_log(cmd_id, status=status, result_json=output_str)

                    # Complete waiting future
                    if cmd_id in self._pending_commands:
                        fut = self._pending_commands.pop(cmd_id)
                        if not fut.done():
                            if self.loop and self.loop.is_running():
                                self.loop.call_soon_threadsafe(
                                    fut.set_result,
                                    {"status": status, "output": output}
                                )
                            else:
                                fut.set_result({"status": status, "output": output})

        except Exception as e:
            logger.info(f"Device '{device_name}' connection ended: {e}")
        finally:
            with self._channel_lock:
                if self._connections.get(device_id) == ws:
                    del self._connections[device_id]
                    self._device_user_map.pop(device_id, None)

            self.db.update_device_status(device_id, status="offline")
            logger.info(f"Device '{device_name}' ({device_id}) marked offline.")

    # -------------------------------------------------------------
    # COMMAND DISPATCHING & ROUTING
    # -------------------------------------------------------------
    async def dispatch_command(
        self,
        user_id: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        target_device_name: Optional[str] = None,
        timeout: float = 25.0
    ) -> Dict[str, Any]:
        """
        Dispatches a command to the target device for user_id.
        Waits for result or timeout.
        Returns a structured result dict.
        """
        params = params or {}

        # Find target online device
        target_device = self.find_target_device(user_id, target_name=target_device_name)
        if not target_device:
            msg = "No Windows device is currently online. Please open the AIVA Device Agent on your computer."
            logger.warning(f"[Dispatch] User {user_id} requested action '{action}', but no device is online.")
            # Record audit log
            cmd_id = str(uuid.uuid4())
            self.db.log_command(
                log_id=cmd_id,
                user_id=user_id,
                device_id=None,
                action=action,
                params_json=json.dumps(params),
                status="offline"
            )
            return {
                "status": "offline",
                "error": "device_offline",
                "message": msg,
                "result": msg
            }

        device_id = target_device["id"]
        device_name = target_device["name"]

        with self._channel_lock:
            ws = self._connections.get(device_id)

        if not ws:
            msg = f"Device '{device_name}' went offline just before command dispatch."
            return {
                "status": "offline",
                "error": "device_offline",
                "message": msg,
                "result": msg
            }

        cmd_id = str(uuid.uuid4())
        cmd_payload = {
            "v": 1,
            "type": "command",
            "id": cmd_id,
            "device_id": device_id,
            "action": action,
            "params": params
        }

        # Create future for awaiting response
        current_loop = asyncio.get_running_loop()
        self.loop = current_loop
        fut = current_loop.create_future()
        self._pending_commands[cmd_id] = fut

        # Log command dispatch in audit table
        self.db.log_command(
            log_id=cmd_id,
            user_id=user_id,
            device_id=device_id,
            action=action,
            params_json=json.dumps(params),
            status="dispatched"
        )

        try:
            # Send message over socket
            ws.send(json.dumps(cmd_payload))
            logger.info(f"[Dispatch] Sent action '{action}' ({cmd_id}) to device '{device_name}'")

            # Await result with timeout
            result = await asyncio.wait_for(fut, timeout=timeout)
            logger.info(f"[Dispatch] Result received for '{action}' ({cmd_id}): {result.get('status')}")
            return {
                "status": result.get("status", "success"),
                "device_id": device_id,
                "device_name": device_name,
                "result": result.get("output", {}).get("message") or result.get("output") or f"{action} completed on {device_name}.",
                "output": result.get("output", {})
            }

        except asyncio.TimeoutError:
            self._pending_commands.pop(cmd_id, None)
            self.db.update_command_log(cmd_id, status="timeout")
            err_msg = f"Command '{action}' sent to '{device_name}' timed out after {timeout} seconds."
            logger.warning(f"[Dispatch Timeout] {err_msg}")
            return {
                "status": "timeout",
                "error": "command_timeout",
                "message": err_msg,
                "result": err_msg
            }
        except Exception as e:
            self._pending_commands.pop(cmd_id, None)
            self.db.update_command_log(cmd_id, status="error", result_json=str(e))
            err_msg = f"Failed to send command to device '{device_name}': {str(e)}"
            logger.error(f"[Dispatch Error] {err_msg}")
            return {
                "status": "error",
                "error": "dispatch_failed",
                "message": err_msg,
                "result": err_msg
            }
