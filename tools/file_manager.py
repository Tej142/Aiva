import os


WORKSPACE_PATH = "workspace"


def create_file(file_name, folder="notes"):

    try:

        folder_path = os.path.join(
            WORKSPACE_PATH,
            folder
        )

        os.makedirs(folder_path, exist_ok=True)

        file_path = os.path.join(
            folder_path,
            file_name
        )

        with open(file_path, "w") as file:
            file.write("")

        return f"File '{file_name}' created successfully."

    except Exception as error:

        return f"[FILE ERROR] {error}"
    
def write_to_file(file_name, content, folder="notes"):

    try:

        folder_path = os.path.join(
            WORKSPACE_PATH,
            folder
        )

        os.makedirs(folder_path, exist_ok=True)

        file_path = os.path.join(
            folder_path,
            file_name
        )

        with open(file_path, "w", encoding="utf-8") as file:

            file.write(content)

        return f"Content written to '{file_name}' successfully."

    except Exception as error:

        return f"[WRITE ERROR] {error}"

def read_file(file_name, folder="notes"):

    try:

        folder_path = os.path.join(
            WORKSPACE_PATH,
            folder
        )

        file_path = os.path.join(
            folder_path,
            file_name
        )

        if not os.path.exists(file_path):

            return "File does not exist."

        with open(file_path, "r", encoding="utf-8") as file:

            content = file.read()

        return content

    except Exception as error:

        return f"[READ ERROR] {error}"
    
def append_to_file(file_name, content, folder="notes"):

    try:

        folder_path = os.path.join(
            WORKSPACE_PATH,
            folder
        )

        os.makedirs(folder_path, exist_ok=True)

        file_path = os.path.join(
            folder_path,
            file_name
        )

        with open(file_path, "a", encoding="utf-8") as file:

            file.write("\n" + content)

        return f"Content appended to '{file_name}' successfully."

    except Exception as error:

        return f"[APPEND ERROR] {error}"