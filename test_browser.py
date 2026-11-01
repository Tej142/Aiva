from tools.browser_tool import google_search

import time


response = google_search(
    "python tutorials"
)

print(response)

input("Press Enter to close browser...")