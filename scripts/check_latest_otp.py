import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "storage/aiva.db"
con = sqlite3.connect(db_path)
row = con.execute("SELECT id, email, created_at, expires_at, used, attempts FROM otps ORDER BY created_at DESC LIMIT 1").fetchone()
print("Latest OTP:", row)
