from flask import Flask, request, jsonify

from core.command_router import detect_command_type
from core.tool_executor import execute_tool
from core.brain import ask_aiva

app = Flask(__name__)


@app.route("/aiva", methods=["POST"])
def aiva_api():

    data = request.json

    user_input = data.get("message", "")

    command_type = detect_command_type(user_input)

    if command_type == "tool":
        response = execute_tool(user_input)

    else:
        response = ask_aiva(user_input)

    return jsonify({
        "response": response
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)