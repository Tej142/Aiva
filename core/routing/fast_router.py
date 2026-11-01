import re


def fast_route(user_input):

    text = user_input.lower().strip()

    # OPEN APPS
    open_patterns = [
        r"open (.+)",
        r"launch (.+)",
        r"start (.+)"
    ]

    for pattern in open_patterns:

        match = re.match(pattern, text)

        if match:

            target = match.group(1).strip()

            return {
                "type": "tool",
                "tool": "open_app",
                "target": target
            }

    # GOOGLE SEARCH
    search_patterns = [
        r"search (.+) on google",
        r"google (.+)"
    ]

    for pattern in search_patterns:

        match = re.match(pattern, text)

        if match:

            target = match.group(1).strip()

            return {
                "type": "tool",
                "tool": "google_search",
                "target": target
            }

    # CREATE FILE
    create_match = re.match(
        r"create file (.+)",
        text
    )

    if create_match:

        target = create_match.group(1).strip()

        return {
            "type": "tool",
            "tool": "create_file",
            "target": target
        }

    # READ FILE
    read_match = re.match(
        r"read file (.+)",
        text
    )

    if read_match:

        target = read_match.group(1).strip()

        return {
            "type": "tool",
            "tool": "read_file",
            "target": target
        }

    return None