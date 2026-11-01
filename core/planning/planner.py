import requests
import json


MODEL_NAME = "qwen2.5-coder:7b"

OLLAMA_URL = "http://localhost:11434/api/generate"


SYSTEM_PROMPT = """
You are the planning engine for AIVA.

Your task:
Analyze the user's request and return ONLY valid JSON.

Possible responses:

For opening applications:
{
    "type": "tool",
    "tool": "open_app",
    "target": "chrome"
}

For Google searches:
{
    "type": "tool",
    "tool": "google_search",
    "target": "python tutorials"
}

For file creation:
{
    "type": "tool",
    "tool": "create_file",
    "target": "notes.txt"
}

For normal conversation:
{
    "type": "chat"
}

Rules:
- Return ONLY JSON
- No markdown
- No explanations
- No extra text
"""


def create_plan(user_input):

    prompt = (
        SYSTEM_PROMPT +
        f"\n\nUser Request: {user_input}"
    )

    try:

        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            }
        )

        data = response.json()

        raw_response = data["response"].strip()

        # REMOVE MARKDOWN WRAPPERS
        raw_response = (
            raw_response
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        print("\n[PLANNER RAW RESPONSE]")
        print(raw_response)

        plan = json.loads(raw_response)

        return plan

    except Exception as error:

        return {
            "type": "error",
            "message": str(error)
        }