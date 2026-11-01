import json
import re

from mistralai import Mistral
from core.shared.logger import get_logger

logger = get_logger("MistralService")


class MistralService:

    def __init__(self):
        with open("config/secrets.json", "r") as file:
            config = json.load(file)

        self.config = config
        self.api_keys = config.get("mistral_api_keys", [])
        if not self.api_keys and config.get("mistral_api_key"):
            self.api_keys = [config.get("mistral_api_key")]

        self.current_key_idx = 0

    def _get_client(self, api_key: str):
        return Mistral(api_key=api_key)

    def _chat_complete(self, messages, model="mistral-small-latest"):
        # 1. Try available Mistral API keys in pool
        for idx in range(len(self.api_keys)):
            key_index = (self.current_key_idx + idx) % len(self.api_keys)
            key = self.api_keys[key_index]
            try:
                client = self._get_client(key)
                response = client.chat.complete(
                    model=model,
                    messages=messages
                )
                self.current_key_idx = key_index  # Keep using working key
                return response.choices[0].message.content
            except Exception as error:
                error_str = str(error)
                if "429" in error_str or "rate_limited" in error_str or "401" in error_str:
                    logger.warning(f"[MistralService] Key #{key_index + 1} rate limited or invalid ({error_str[:60]}...). Rotating to next key...")
                else:
                    logger.warning(f"[MistralService Error on Key #{key_index + 1}] {error}")

        # Fallback to Gemini if Mistral fails/rate limits
        try:
            from google import genai
            gemini_client = genai.Client(api_key=self.config.get("gemini_api_key"))
            prompt_text = ""
            for msg in messages:
                prompt_text += f"{msg.get('role', 'user').upper()}:\n{msg.get('content', '')}\n\n"
            response = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt_text
            )
            print("[MistralService] Used Gemini fallback successfully.")
            return response.text
        except Exception as gemini_err:
            print("[MistralService Gemini Fallback Error]", gemini_err)

        # Fallback to Groq
        try:
            from groq import Groq
            groq_client = Groq(api_key=self.config.get("groq_api_key"))
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=messages
            )
            print("[MistralService] Used Groq fallback successfully.")
            return response.choices[0].message.content
        except Exception as groq_err:
            print("[MistralService Groq Fallback Error]", groq_err)
            raise RuntimeError("All LLM providers failed in MistralService.")

    async def generate_workflow(
        self,
        goal
    ):
        # ----------------------------------------------------
        # FAST PRE-PARSER (ONLY FOR SIMPLE SINGLE-ACTION GOALS)
        # Compound goals with 'and', 'then', 'also', 'after' go directly to LLM Planner
        # ----------------------------------------------------
        from urllib.parse import quote_plus
        g = goal.lower().strip()
        is_compound = any(sep in g for sep in [" and ", " then ", " also ", " after ", " & ", ", "]) or ("create" in g and "music" in g)

        if not is_compound:
            # 1. YouTube Search (English, Telugu, Tanglish)
            if any(k in g for k in ['youtube', 'à°¯à±‚à°Ÿà±à°¯à±‚à°¬à±']) and any(k in g for k in ['search', 'vethuku', 'find', 'look', 'play', 'à°²à±‹', 'lo']):
                q = g
                for w in ['open youtube', 'youtube lo', 'youtube', 'à°¯à±‚à°Ÿà±à°¯à±‚à°¬à±', 'search for', 'search', 'vethuku', 'in youtube', 'on youtube', 'play', 'cheyyi']:
                    q = q.replace(w, '')
                q = q.strip(' ,').strip()
                if q:
                    clean_q = re.sub(r'[^a-zA-Z0-9]', '_', q)
                    return {
                        'goal': goal,
                        'workflow': [{
                            'node_id': f'search_{clean_q}_in_youtube',
                            'action': 'open_url',
                            'parameters': {'url': f'https://www.youtube.com/results?search_query={quote_plus(q)}'},
                            'dependencies': []
                        }]
                    }

            # 2. Apple Music Launch & Play (Native App)
            if any(k in g for k in ['apple music', 'applemusic', 'play beat it', 'play song', 'play music', 'beat it']):
                return {
                    'goal': goal,
                    'workflow': [
                        {
                            'node_id': 'launch_apple_music_app',
                            'action': 'launch_app',
                            'parameters': {'app_name': 'apple music'},
                            'dependencies': []
                        },
                        {
                            'node_id': 'play_media_task',
                            'action': 'media_control',
                            'parameters': {'media_action': 'play'},
                            'dependencies': ['launch_apple_music_app']
                        }
                    ]
                }

            # 3. Google Search (English, Telugu, Tanglish)
            if any(k in g for k in ['google', 'à°—à±‚à°—à±à°²à±']) and any(k in g for k in ['search', 'vethuku', 'find', 'look', 'à°²à±‹', 'lo']):
                q = g
                for w in ['open google', 'google lo', 'google', 'à°—à±‚à°—à±à°²à±', 'search for', 'search', 'vethuku', 'in google', 'on google', 'cheyyi']:
                    q = q.replace(w, '')
                q = q.strip(' ,').strip()
                if q:
                    clean_q = re.sub(r'[^a-zA-Z0-9]', '_', q)
                    return {
                        'goal': goal,
                        'workflow': [{
                            'node_id': f'search_{clean_q}_in_google',
                            'action': 'open_url',
                            'parameters': {'url': f'https://www.google.com/search?q={quote_plus(q)}'},
                            'dependencies': []
                        }]
                    }

            # 4. App Launching (English, Telugu, Tanglish)
            for app in ['notepad', 'vscode', 'vs code', 'calculator', 'cmd', 'terminal', 'paint', 'chrome', 'apple music', 'youtube']:
                if app in g and any(k in g for k in ['open', 'launch', 'cheyyi', 'à°“à°ªà±†à°¨à±']):
                    clean_app = re.sub(r'[^a-zA-Z0-9]', '_', app)
                    return {
                        'goal': goal,
                        'workflow': [{
                            'node_id': f'launch_{clean_app}',
                            'action': 'launch_app',
                            'parameters': {'app_name': app},
                            'dependencies': []
                        }]
                    }

            # 5. Volume Control (English, Telugu, Tanglish)
            if any(k in g for k in ['volume', 'à°µà°¾à°²à±à°¯à±‚à°®à±', 'sound', 'à°¸à±Œà°‚à°¡à±']):
                delta = 20 if any(k in g for k in ['increase', 'up', 'penchu', 'à°ªà±†à°‚à°šà±', 'ekkuva']) else (-20 if any(k in g for k in ['decrease', 'down', 'tagginchu', 'à°¤à°—à±à°—à°¿à°‚à°šà±', 'thagginchu']) else 0)
                if 'mute' in g or 'à°®à±à°¯à±‚à°Ÿà±' in g:
                    return {
                        'goal': goal,
                        'workflow': [{'node_id': 'mute_volume', 'action': 'adjust_volume', 'parameters': {'action': 'mute'}, 'dependencies': []}]
                    }
                elif delta != 0:
                    return {
                        'goal': goal,
                        'workflow': [{'node_id': 'change_volume', 'action': 'adjust_volume', 'parameters': {'delta': delta}, 'dependencies': []}]
                    }

            # 6. Media Control
            if any(k in g for k in ['pause', 'stop', 'aapu', 'à°†à°ªà±']) and any(k in g for k in ['music', 'song', 'media']):
                return {
                    'goal': goal,
                    'workflow': [{'node_id': 'media_control_task', 'action': 'media_control', 'parameters': {'media_action': 'pause'}, 'dependencies': []}]
                }

            # 7. Take Screenshot
            if any(k in g for k in ['screenshot', 'screen shot', 'à°¸à±à°•à±à°°à±€à°¨à±â€Œà°·à°¾à°Ÿà±', 'screen capture']):
                return {
                    'goal': goal,
                    'workflow': [{'node_id': 'take_screenshot_task', 'action': 'take_screenshot', 'parameters': {}, 'dependencies': []}]
                }

            # 8. System Power & Workstation Lock
            if any(k in g for k in ['lock', 'à°²à°¾à°•à±']) and any(k in g for k in ['system', 'computer', 'screen', 'pc', 'à°¸à°¿à°¸à±à°Ÿà°®à±']):
                return {
                    'goal': goal,
                    'workflow': [{'node_id': 'lock_workstation_task', 'action': 'system_power', 'parameters': {'power_action': 'lock'}, 'dependencies': []}]
                }

        # ----------------------------------------------------
        # LLM WORKFLOW GENERATION WITH PROMPT
        # ----------------------------------------------------
        system_prompt = """
You are AIVA's master autonomous workflow planner.
Deconstruct compound and single-step user goals into executable dependency graphs.
Return ONLY valid JSON.

Allowed actions:
- launch_app (parameter: "app_name") -> launches native Windows applications and desktop web apps (e.g. "apple music", "youtube", "chrome", "notepad", "calculator", "vscode", "whatsapp")
  * IMPORTANT: When user wants to play music or open Apple Music, ALWAYS use launch_app with "apple music" (NEVER open_url in browser).
- media_control (parameters: "media_action": "play" / "pause" / "next" / "previous" / "play_pause")
- create_document (parameters: "file_name", "topic", "content") -> creates formatted Microsoft Word .docx files
- open_url (parameter: "url") -> opens web URLs
  * For YouTube search: "https://www.youtube.com/results?search_query=<query>"
  * For Google search: "https://www.google.com/search?q=<query>"
  * For WhatsApp Web: "https://web.whatsapp.com/"
- focus_window (parameter: "title") -> brings an application or browser window to the foreground (e.g. "chrome", "whatsapp", "notepad")
- type_text (parameter: "text", optional: "instant_paste": true) -> types or pastes text into the active input box
- press_key (parameter: "key": "enter" / "tab" / "esc" / "backspace" / "up" / "down", optional: "count": 1) -> presses a keyboard key
- hotkey (parameter: "keys": ["ctrl", "f"] or ["ctrl", "alt", "/"] or ["alt", "tab"]) -> triggers keyboard shortcuts
- wait (parameter: "seconds": number) -> pauses for app/page to load
- adjust_volume (parameters: "action": "mute" or "delta": 10 or "level": 50)
- take_screenshot (parameters: {})
- inspect_screen (parameters: "query": "what error is on screen" / "describe open windows") -> uses vision AI to analyze screen and answer user questions
- visual_click (parameters: "target": "description of button/icon/text to click", "action_type": "click" / "double_click" / "right_click") -> visually locates UI element on screen and clicks it
- visual_type (parameters: "target": "search bar / input field description", "text": "text to type", "press_enter": true) -> clicks visual field and types text
- system_power (parameters: "power_action": "lock")
- research (parameter: "query")
- web_automation (parameter: "instruction") -> navigates to an external website, fills out forms with provided data (login, search, result lookup), and reads/returns the result. Use this action when the user provides a URL along with form data like IDs, PINs, months, semesters, etc. The "instruction" parameter should be the FULL original user message containing the URL and all form data.

Return JSON in EXACTLY this format:
{
    "goal": "original user goal",
    "workflow": [
        {
            "node_id": "step_1_unique_name",
            "action": "action_name",
            "parameters": { ... },
            "dependencies": []
        },
        {
            "node_id": "step_2_unique_name",
            "action": "action_name",
            "parameters": { ... },
            "dependencies": ["step_1_unique_name"]
        }
    ]
}

Examples:

User: play beat it song
Output:
{
    "goal": "play beat it song",
    "workflow": [
        {
            "node_id": "launch_apple_music_app",
            "action": "launch_app",
            "parameters": {
                "app_name": "apple music"
            },
            "dependencies": []
        },
        {
            "node_id": "play_song",
            "action": "media_control",
            "parameters": {
                "media_action": "play"
            },
            "dependencies": ["launch_apple_music_app"]
        }
    ]
}

User: open chrome and open whatsapp in it and message hi to pujiii
Output:
{
    "goal": "open chrome and open whatsapp in it and message hi to pujiii",
    "workflow": [
        {
            "node_id": "open_whatsapp_web",
            "action": "open_url",
            "parameters": {
                "url": "https://web.whatsapp.com/"
            },
            "dependencies": []
        },
        {
            "node_id": "wait_for_whatsapp_load",
            "action": "wait",
            "parameters": {
                "seconds": 7.0
            },
            "dependencies": ["open_whatsapp_web"]
        },
        {
            "node_id": "focus_whatsapp_window",
            "action": "focus_window",
            "parameters": {
                "title": "WhatsApp"
            },
            "dependencies": ["wait_for_whatsapp_load"]
        },
        {
            "node_id": "reset_active_search",
            "action": "press_key",
            "parameters": {
                "key": "esc",
                "count": 2
            },
            "dependencies": ["focus_whatsapp_window"]
        },
        {
            "node_id": "focus_whatsapp_search",
            "action": "hotkey",
            "parameters": {
                "keys": ["ctrl", "alt", "/"]
            },
            "dependencies": ["reset_active_search"]
        },
        {
            "node_id": "type_contact_name",
            "action": "type_text",
            "parameters": {
                "text": "pujiii"
            },
            "dependencies": ["focus_whatsapp_search"]
        },
        {
            "node_id": "wait_for_search_results",
            "action": "wait",
            "parameters": {
                "seconds": 2.0
            },
            "dependencies": ["type_contact_name"]
        },
        {
            "node_id": "highlight_top_result",
            "action": "press_key",
            "parameters": {
                "key": "down"
            },
            "dependencies": ["wait_for_search_results"]
        },
        {
            "node_id": "select_contact",
            "action": "press_key",
            "parameters": {
                "key": "enter"
            },
            "dependencies": ["highlight_top_result"]
        },
        {
            "node_id": "wait_chat_ready",
            "action": "wait",
            "parameters": {
                "seconds": 2.0
            },
            "dependencies": ["select_contact"]
        },
        {
            "node_id": "type_message_content",
            "action": "type_text",
            "parameters": {
                "text": "hi"
            },
            "dependencies": ["wait_chat_ready"]
        },
        {
            "node_id": "send_message",
            "action": "press_key",
            "parameters": {
                "key": "enter"
            },
            "dependencies": ["type_message_content"]
        }
    ]
}

User: create a word file add some notes about the basics of python in that and open apple music and then play the beat it song
Output:
{
    "goal": "create a word file add some notes about the basics of python in that and open apple music and then play the beat it song",
    "workflow": [
        {
            "node_id": "create_python_notes_doc",
            "action": "create_document",
            "parameters": {
                "file_name": "python_basics.docx",
                "topic": "Basics of Python"
            },
            "dependencies": []
        },
        {
            "node_id": "launch_apple_music_app",
            "action": "launch_app",
            "parameters": {
                "app_name": "apple music"
            },
            "dependencies": ["create_python_notes_doc"]
        },
        {
            "node_id": "play_song",
            "action": "media_control",
            "parameters": {
                "media_action": "play"
            },
            "dependencies": ["launch_apple_music_app"]
        }
    ]
}

User: Open YouTube and search machine learning
Output:
{
    "goal": "Open YouTube and search machine learning",
    "workflow": [
        {
            "node_id": "search_youtube_ml",
            "action": "open_url",
            "parameters": {
                "url": "https://www.youtube.com/results?search_query=machine+learning"
            },
            "dependencies": []
        }
    ]
}

User: open chrome and search for pythagoras and note the key points in a new word file
Output:
{
    "goal": "open chrome and search for pythagoras and note the key points in a new word file",
    "workflow": [
        {
            "node_id": "search_pythagoras_in_chrome",
            "action": "open_url",
            "parameters": {
                "url": "https://www.google.com/search?q=pythagoras"
            },
            "dependencies": []
        },
        {
            "node_id": "create_pythagoras_notes_doc",
            "action": "create_document",
            "parameters": {
                "file_name": "pythagoras_notes.docx",
                "topic": "Pythagoras Key Points",
                "content": "Key notes and biographical summary for Pythagoras"
            },
            "dependencies": []
        }
    ]
}

User: what is currently on my screen?
Output:
{
    "goal": "what is currently on my screen?",
    "workflow": [
        {
            "node_id": "inspect_current_screen",
            "action": "inspect_screen",
            "parameters": {
                "query": "Describe what is currently visible on the screen and any active applications or errors."
            },
            "dependencies": []
        }
    ]
}

User: click on the blue download button
Output:
{
    "goal": "click on the blue download button",
    "workflow": [
        {
            "node_id": "click_blue_download_button",
            "action": "visual_click",
            "parameters": {
                "target": "blue download button",
                "action_type": "click"
            },
            "dependencies": []
        }
    ]
}

User: https://sbtet.ap.gov.in/APSBTET/gradeWiseResults.do check my result pin is 24155-CM-002 month MA26 semester 4
Output:
{
    "goal": "check diploma result on sbtet portal",
    "workflow": [
        {
            "node_id": "check_sbtet_result",
            "action": "web_automation",
            "parameters": {
                "instruction": "https://sbtet.ap.gov.in/APSBTET/gradeWiseResults.do check my result pin is 24155-CM-002 month MA26 semester 4"
            },
            "dependencies": []
        }
    ]
}

User: go to https://results.example.com and check my result roll number is 12345 exam month is june year 2024
Output:
{
    "goal": "check result on results portal",
    "workflow": [
        {
            "node_id": "check_online_result",
            "action": "web_automation",
            "parameters": {
                "instruction": "https://results.example.com check my result roll number is 12345 exam month is june year 2024"
            },
            "dependencies": []
        }
    ]
}
"""

        messages = [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": goal
            }
        ]

        content = self._chat_complete(messages)

        print(
            "\n=== MISTRAL RESPONSE ==="
        )

        print(content)

        print(
            "========================\n"
        )

        try:

            return json.loads(
                content
            )

        except:

            cleaned = re.sub(
                r"```json|```",
                "",
                content
            ).strip()

            return json.loads(
                cleaned
            )

    async def summarize_research(
        self,
        query,
        search_results
    ):

        content = ""

        for result in search_results:

            content += (
                f"Title: {result.get('title','')}\n"
                f"URL: {result.get('url','')}\n"
                f"Content: {result.get('content','')}\n\n"
            )

        messages = [

            {
                "role": "system",
                "content": """
You are AIVA's research engine.

Create a concise factual report.

Include:
- Summary
- Important Details
- Key Facts
- Sources
"""
            },

            {
                "role": "user",
                "content": content
            }

        ]

        return self._chat_complete(messages)
    
    async def answer_from_memory(
        self,
        user_query,
        memory_content
    ):

        messages = [

            {
                "role": "system",
                "content": """
    You are AIVA.

    Answer the user's question using ONLY the memory provided.

    Rules:

    - Do not invent information.
    - Do not use external knowledge.
    - If the memory does not contain enough information,
    say so.
    - Adapt the answer length according to the question.
    - If the user asks for a short answer,
    keep it short.
    - If the user asks for detailed explanation,
    explain in detail.
    """
            },

            {
                "role": "user",
                "content": f"""
    USER QUESTION:

    {user_query}

    MEMORY:

    {memory_content}
    """
            }

        ]

        return self._chat_complete(messages)

    async def extract_knowledge(
        self,
        query,
        report
    ):

        messages = [

            {
                "role":"system",
                "content":"""
Return ONLY JSON.

{
    "entity":"",
    "type":"",
    "summary":"",
    "facts":[]
}
"""
            },

            {
                "role":"user",
                "content":f"""
Query:

{query}

Report:

{report}
"""
            }

        ]

        content = self._chat_complete(messages)

        try:

            return json.loads(
                content
            )

        except:

            cleaned = re.sub(
                r"```json|```",
                "",
                content
            ).strip()

            return json.loads(
                cleaned
            )
