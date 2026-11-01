from tools.app_discovery import discover_apps


apps = discover_apps()

print("\n===== DISCOVERED APPS =====\n")

count = 0

for app_name in apps:

    print(app_name)

    count += 1

    if count >= 50:
        break