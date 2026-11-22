"""Export thumbs-down feedback items into candidate golden YAML entries for review.

Usage:
    python scripts/failures_to_golden.py
    python scripts/failures_to_golden.py --user-id <user_id>
    python scripts/failures_to_golden.py --output tests/eval/my_candidates.yaml
    python scripts/failures_to_golden.py --stdout

Rules:
- Never auto-merge into golden_commands.yaml.
- Exports only thumbs-down failure records.
- Preserves masked phone numbers and redacted secrets.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch_feedback_failures(db_path: str, user_id: str = "") -> List[Dict[str, Any]]:
    """Retrieve thumbs-down feedback items from SQLite database."""
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}", file=sys.stderr)
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Verify agent_feedback table exists
    tbl_check = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_feedback'"
    ).fetchone()
    if not tbl_check:
        conn.close()
        return []

    query = """
        SELECT id, user_id, user_message, assistant_reply, tier, tag, tools, note, rating, created_at
        FROM agent_feedback
        WHERE rating = 'thumbs_down'
    """
    params: List[Any] = []
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    query += " ORDER BY created_at ASC"

    rows = cursor.execute(query, tuple(params)).fetchall()
    results = []
    for r in rows:
        tools_list = []
        try:
            tools_list = json.loads(r["tools"]) if r["tools"] else []
        except Exception:
            tools_list = [t.strip() for t in str(r["tools"]).split(",") if t.strip()]

        results.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "user_message": r["user_message"],
            "assistant_reply": r["assistant_reply"],
            "tier": r["tier"] or 1,
            "tag": r["tag"] or "conversation",
            "tools": tools_list,
            "note": r["note"] or "",
            "created_at": r["created_at"],
        })
    conn.close()
    return results


def export_to_candidate_yaml(items: List[Dict[str, Any]], output_path: str = "", to_stdout: bool = False) -> str:
    """Format candidate golden cases and write to file or return as string."""
    now_str = datetime.datetime.utcnow().isoformat() + "Z"
    lines = [
        "# ==============================================================================",
        "# CANDIDATE GOLDEN CASES (Exported from agent_feedback)",
        f"# Generated: {now_str}",
        f"# Total failure records: {len(items)}",
        "#",
        "# INSTRUCTIONS FOR REVIEW:",
        "# 1. Review each candidate query below.",
        "# 2. Specify correct expectations (expect_tier, expect_route, expect_tools_any, etc.).",
        "# 3. Manually copy verified cases into tests/eval/golden_commands.yaml.",
        "# 4. NEVER auto-merge unverified failure candidates directly into CI golden dataset.",
        "# ==============================================================================",
        "",
        "cases:",
    ]

    for item in items:
        clean_query = item["user_message"].replace('"', '\\"')
        lines.append(f'  - id: candidate_fb_{item["id"]}')
        lines.append('    category: failure_candidate')
        lines.append(f'    query: "{clean_query}"')
        lines.append('    channel: hud')
        lines.append(f'    expect_tier: {item["tier"]}')
        lines.append('    expect_route: task' if item.get("tools") else '    expect_route: conversation')
        if item.get("tools"):
            tools_str = ", ".join(f'"{t}"' for t in item["tools"])
            lines.append(f'    # prior_tools_recorded: [{tools_str}]')
        if item.get("note"):
            clean_note = item["note"].replace("\n", " ").strip()
            lines.append(f'    # user_note: "{clean_note}"')
        if item.get("assistant_reply"):
            clean_snippet = item["assistant_reply"].replace("\n", " ").strip()[:100]
            lines.append(f'    # prior_reply_snippet: "{clean_snippet}..."')
        lines.append(f'    # reported_at: {item["created_at"]}')
        lines.append("")

    yaml_text = "\n".join(lines)

    if to_stdout:
        print(yaml_text)
    elif output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        print(f"Exported {len(items)} failure candidate(s) to {output_path}")

    return yaml_text


def main():
    parser = argparse.ArgumentParser(description="Export thumbs-down agent failures to candidate golden YAML.")
    parser.add_argument(
        "--db",
        default=os.path.join(ROOT_DIR, "storage", "aiva.db"),
        help="Path to SQLite database (default: storage/aiva.db)",
    )
    parser.add_argument(
        "--user-id",
        default="",
        help="Filter by specific user_id (optional, default: all users)",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(ROOT_DIR, "tests", "eval", "candidate_golden.yaml"),
        help="Path for candidate YAML export (default: tests/eval/candidate_golden.yaml)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print YAML directly to stdout instead of saving to file.",
    )
    args = parser.parse_args()

    items = fetch_feedback_failures(args.db, user_id=args.user_id)
    export_to_candidate_yaml(items, output_path=args.output, to_stdout=args.stdout)


if __name__ == "__main__":
    main()

