import sys
import requests

base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
email = sys.argv[2] if len(sys.argv) > 2 else "charante153624@gmail.com"

print(f"Testing login at {base_url} with {email}")
resp = requests.post(f"{base_url}/api/auth/login", json={"email": email}, timeout=10)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text}")

