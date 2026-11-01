from tools.app_launcher import open_application

from tools.browser_tool import google_search

from tools.file_manager import (
    create_file,
    write_to_file,
    read_file,
    append_to_file
)


def execute_tool(command_data):

    tool = command_data.get("tool")

    target = command_data.get("target", "")

    # OPEN APPLICATION
    if tool == "open_app":

        return open_application(target)

    # GOOGLE SEARCH
    elif tool == "google_search":

        return google_search(target)

    # CREATE FILE
    elif tool == "create_file":

        if not target.endswith(".txt"):

            target += ".txt"

        return create_file(target)

    # READ FILE
    elif tool == "read_file":

        if not target.endswith(".txt"):

            target += ".txt"

        return read_file(target)

    return "Unknown tool."