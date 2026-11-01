def detect_command_type(user_input):
    """
    Detects what type of request
    the user is making.
    """

    user_input = user_input.lower()

    tool_keywords = [
        "open",
        "launch",
        "start",
        "close",
        "search",
        "create",
        "delete",
        "write",
        "read",
        "add",
        "save"
    ]

    for keyword in tool_keywords:

        if keyword in user_input:
            return "tool"

    return "chat"