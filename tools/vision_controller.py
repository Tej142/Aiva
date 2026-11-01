import time
import asyncio
from typing import Optional, Dict, Any
try:
    import pyautogui
except Exception:
    pyautogui = None
from services.vision_service import VisionService
from core.shared.logger import get_logger

logger = get_logger("VisionController")

# Shared VisionService instance
_vision_service = VisionService()


async def inspect_screen(query: str = "Describe what is currently visible on the screen.") -> Dict[str, Any]:
    """
    Analyzes the user's desktop screen using vision models and returns an intelligent summary/answer.
    """
    logger.info(f"Inspecting screen for query: '{query}'")
    return await _vision_service.inspect_screen(query)


async def visual_click(
    target_description: str,
    action_type: str = "click",
    offset_x: int = 0,
    offset_y: int = 0
) -> Dict[str, Any]:
    """
    Visually locates a target UI element on screen (by text, description, icon color/shape)
    and executes a mouse click/double_click/right_click at its center.
    """
    logger.info(f"Visual click requested for target: '{target_description}' (action: {action_type})")

    location_res = await _vision_service.locate_element(target_description)
    if location_res.get("status") != "success":
        msg = f"Failed to visually locate '{target_description}': {location_res.get('reason', location_res.get('message', 'Unknown error'))}"
        logger.warning(msg)
        return {"status": "error", "message": msg}

    target_x = location_res["pixel_x"] + offset_x
    target_y = location_res["pixel_y"] + offset_y

    try:
        # Move smoothly to the target position
        pyautogui.moveTo(target_x, target_y, duration=0.25)
        time.sleep(0.05)

        if action_type == "double_click":
            pyautogui.doubleClick(target_x, target_y)
        elif action_type == "right_click":
            pyautogui.rightClick(target_x, target_y)
        else:
            pyautogui.click(target_x, target_y)

        time.sleep(0.1)
        msg = f"Successfully clicked '{target_description}' at ({target_x}, {target_y})"
        logger.info(msg)
        return {
            "status": "success",
            "message": msg,
            "coordinates": (target_x, target_y),
            "element_info": location_res.get("element_info", "")
        }

    except Exception as e:
        logger.error(f"Error performing visual click at ({target_x}, {target_y}): {e}")
        return {"status": "error", "message": str(e)}


async def visual_type(
    target_description: str,
    text: str,
    press_enter: bool = True
) -> Dict[str, Any]:
    """
    Visually locates an input field/search bar, clicks inside it, and pastes text.
    """
    click_res = await visual_click(target_description, action_type="click")
    if click_res.get("status") != "success":
        return click_res

    time.sleep(0.2)
    from tools.ui_controller import type_text, press_key

    type_res = type_text(text, instant_paste=True)
    if press_enter:
        time.sleep(0.1)
        press_key("enter")

    return {
        "status": "success",
        "message": f"Typed '{text}' into '{target_description}'",
        "click_result": click_res,
        "type_result": type_res
    }
