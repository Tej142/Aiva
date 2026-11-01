import requests

response = requests.post(
    "https://purr-stitch-easeful.ngrok-free.dev/aiva",
    json={
        "message": "open chrome"
    }
)

print(response.json())