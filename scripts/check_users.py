import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else 'storage/aiva.db'
con = sqlite3.connect(db_path)
print("=== USERS ===")
for row in con.execute('SELECT id, email, name, gate_state, is_active FROM users').fetchall():
    print(row)
print("=== DEVICES ===")
for row in con.execute('SELECT id, user_id, name, platform, status FROM devices').fetchall():
    print(row)

