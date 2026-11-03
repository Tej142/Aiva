import io
import os
import re
import json
import time
import platform
from typing import Optional, Dict, Any, Tuple
from PIL import Image
try:
    import pyautogui
except Exception:
    pyautogui = None

from google import genai
from google.genai import types
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger

logger = get_logger("VisionService")


class VisionService:
    """
    High-performance Computer Vision & Screen Understanding Service for AIVA.
    Uses Google Gemini Vision (gemini-2.5-flash) for instant screen analysis,
    OCR, and UI element localization (finding buttons, icons, text fields).
    """

    def __init__(self):
        secrets = load_secrets()
        self.api_keys = secrets.get("gemini_api_keys", [])
        if not self.api_keys and secrets.get("gemini_api_key"):
            self.api_keys = [secrets.get("gemini_api_key")]
        self.current_key_idx = 0

    def _get_client(self, api_key: str):
        return genai.Client(api_key=api_key)

    def _grab_screen_win32_gdi(self) -> Image.Image:
        """High-speed native Windows GDI screen capture."""
        import ctypes
        from ctypes import windll, wintypes, Structure, c_long, byref

        user32 = windll.user32
        gdi32 = windll.gdi32

        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)

        hdesktop = user32.GetDesktopWindow()
        desktop_dc = user32.GetDC(hdesktop)
        mem_dc = gdi32.CreateCompatibleDC(desktop_dc)
        bitmap = gdi32.CreateCompatibleBitmap(desktop_dc, width, height)
        gdi32.SelectObject(mem_dc, bitmap)

        # 0x00CC0020 = SRCCOPY
        gdi32.BitBlt(mem_dc, 0, 0, width, height, desktop_dc, 0, 0, 0x00CC0020)

        class BITMAPINFOHEADER(Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD),
                ('biWidth', c_long),
                ('biHeight', c_long),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', c_long),
                ('biYPelsPerMeter', c_long),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, byref(bmi), 0)

        # Cleanup GDI handles
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hdesktop, desktop_dc)

        img = Image.frombuffer('RGBA', (width, height), buf, 'raw', 'BGRA', 0, 1)
        return img.convert('RGB')

    def capture_screen(self, region: Optional[Tuple[int, int, int, int]] = None) -> Image.Image:
        """
        Captures the current desktop screen with native GDI and PIL/pyautogui fallbacks.
        """
        # Try native Windows GDI first (fastest and handles all desktop permissions)
        if platform.system() == "Windows":
            try:
                img = self._grab_screen_win32_gdi()
                if region:
                    left, top, width, height = region
                    return img.crop((left, top, left + width, top + height))
                return img
            except Exception as e:
                logger.warning(f"Native Win32 GDI grab failed, trying pyautogui: {e}")

        # Fallback to PyAutoGUI
        try:
            if region:
                return pyautogui.screenshot(region=region)
            return pyautogui.screenshot()
        except Exception as e:
            logger.warning(f"PyAutoGUI grab failed, trying PIL ImageGrab: {e}")

        # Fallback to PIL ImageGrab
        from PIL import ImageGrab
        img = ImageGrab.grab()
        if region:
            left, top, width, height = region
            return img.crop((left, top, left + width, top + height))
        return img

    def image_to_bytes(self, image: Image.Image, format: str = "JPEG") -> bytes:
        """Converts PIL Image to bytes."""
        buffer = io.BytesIO()
        if format.upper() in ("JPEG", "JPG") and image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        image.save(buffer, format=format, quality=85)
        return buffer.getvalue()

    async def inspect_screen(self, query: str = "Describe what is currently visible on the screen.") -> Dict[str, Any]:
        """
        Takes a screenshot and answers questions about what is on screen using Gemini Vision.
        """
        if not self.api_keys:
            return {"status": "error", "message": "No Gemini API keys configured"}

        try:
            last_sc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "last_screenshot.png")
            if os.path.exists(last_sc) and (time.time() - os.path.getmtime(last_sc) < 60):
                try:
                    screenshot = Image.open(last_sc)
                except Exception:
                    screenshot = self.capture_screen()
            else:
                screenshot = self.capture_screen()
            img_bytes = self.image_to_bytes(screenshot, format="JPEG")

            prompt = f"""
You are AIVA, the user's high-intelligence AI desktop assistant (like JARVIS).
The user is asking via voice: "{query}"

CRITICAL VOICE RULES:
1. Answer in 1 to 2 smart, natural, conversational sentences.
2. Give a sharp, high-level summary of the primary active window and task (e.g., "You currently have the Antigravity IDE open working on the AIVA project.", "You're viewing the Oracle Cloud setup documentation in VS Code.").
3. DO NOT list every single button, menu, coordinate, or text element. Be intelligent, concise, and executive.
4. If there is an error on screen that the user asks about, identify the root cause in one direct sentence.
5. Plain conversational text only (no markdown symbols, no bullet points).
"""

            for k_idx in range(len(self.api_keys)):
                key_index = (self.current_key_idx + k_idx) % len(self.api_keys)
                key = self.api_keys[key_index]
                client = self._get_client(key)

                for model_name in ["gemini-3.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash"]:
                    try:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=[
                                types.Part.from_bytes(
                                    data=img_bytes,
                                    mime_type="image/jpeg",
                                ),
                                prompt
                            ]
                        )
                        result_text = response.text.strip() if response.text else "No content detected."
                        self.current_key_idx = key_index
                        return {
                            "status": "success",
                            "query": query,
                            "analysis": result_text
                        }
                    except Exception as model_err:
                        logger.warning(f"Vision model {model_name} on Key #{key_index+1} failed: {model_err}")

            return {"status": "error", "message": "All vision models and keys failed to respond."}

        except Exception as e:
            logger.error(f"Error inspecting screen: {e}")
            return {"status": "error", "message": str(e)}

    async def locate_element(self, target_description: str) -> Dict[str, Any]:
        """
        Locates a UI element (button, icon, input box, link, text) on screen using Gemini Vision.
        Returns pixel coordinates (x, y) suitable for clicking.
        """
        if not self.api_keys:
            return {"status": "error", "message": "No Gemini API keys configured"}

        try:
            screenshot = self.capture_screen()
            screen_width, screen_height = screenshot.size
            img_bytes = self.image_to_bytes(screenshot, format="JPEG")

            prompt = f"""
You are a high-precision UI locator for desktop automation.
Locate the following UI element on the provided screen image:
Target Element: "{target_description}"

Return ONLY a JSON object in this exact format:
{{
  "found": true,
  "confidence": 0.95,
  "box_2d": [ymin, xmin, ymax, xmax],
  "point": [center_y, center_x],
  "description": "Short description of the identified element"
}}

Note: Coordinates in box_2d and point MUST be normalized integers/floats on a 0 to 1000 scale (where 0,0 is top-left and 1000,1000 is bottom-right).
If the element cannot be found, return {{"found": false, "reason": "Element not visible"}}.
Do NOT output markdown blocks or code fences, only valid JSON.
"""

            response = None
            for k_idx in range(len(self.api_keys)):
                key_index = (self.current_key_idx + k_idx) % len(self.api_keys)
                key = self.api_keys[key_index]
                client = self._get_client(key)

                for model_name in ["gemini-3.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash"]:
                    try:
                        response = client.models.generate_content(
                            model=model_name,
                            contents=[
                                types.Part.from_bytes(
                                    data=img_bytes,
                                    mime_type="image/jpeg",
                                ),
                                prompt
                            ]
                        )
                        self.current_key_idx = key_index
                        break
                    except Exception as model_err:
                        logger.warning(f"Locate model {model_name} on Key #{key_index+1} failed: {model_err}")
                if response:
                    break

            if not response:
                return {"status": "error", "message": "All vision models failed to locate element."}

            raw_text = response.text.strip() if response.text else ""
            cleaned_text = re.sub(r"^```json\s*", "", raw_text)
            cleaned_text = re.sub(r"\s*```$", "", cleaned_text).strip()

            data = json.loads(cleaned_text)

            if not data.get("found"):
                return {
                    "status": "not_found",
                    "target": target_description,
                    "reason": data.get("reason", "Element not located on screen")
                }

            # Convert 0-1000 normalized coordinates to real screen pixels
            point = data.get("point")
            if point and len(point) == 2:
                norm_y, norm_x = point
            elif "box_2d" in data and len(data["box_2d"]) == 4:
                ymin, xmin, ymax, xmax = data["box_2d"]
                norm_y = (ymin + ymax) / 2
                norm_x = (xmin + xmax) / 2
            else:
                return {"status": "error", "message": "Invalid coordinates returned by model"}

            pixel_x = int((norm_x / 1000.0) * screen_width)
            pixel_y = int((norm_y / 1000.0) * screen_height)

            return {
                "status": "success",
                "target": target_description,
                "pixel_x": pixel_x,
                "pixel_y": pixel_y,
                "screen_size": (screen_width, screen_height),
                "confidence": data.get("confidence", 1.0),
                "element_info": data.get("description", "")
            }

        except Exception as e:
            logger.error(f"Error locating element '{target_description}': {e}")
            return {"status": "error", "message": str(e)}

