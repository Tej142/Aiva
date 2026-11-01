import os

from rapidfuzz import process


START_MENU_PATHS = [
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",

    os.path.expandvars(
        r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"
    )
]


def discover_apps():

    discovered_apps = {}

    for start_path in START_MENU_PATHS:

        for root, dirs, files in os.walk(start_path):

            for file in files:

                if file.endswith(".lnk"):

                    app_name = file.replace(".lnk", "").lower()

                    full_path = os.path.join(root, file)

                    discovered_apps[app_name] = full_path

    return discovered_apps


def find_best_app_match(user_input):

    apps = discover_apps()

    app_names = list(apps.keys())

    best_match = process.extractOne(
        user_input.lower(),
        app_names
    )

    if best_match:

        matched_name = best_match[0]

        similarity_score = best_match[1]

        if similarity_score >= 60:

            return matched_name, apps[matched_name]

    return None, None