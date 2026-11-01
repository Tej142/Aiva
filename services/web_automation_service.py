"""
WebAutomationService — Direct Backend Site Operations for AIVA.

Communicates directly with website backends via HTTP (GET/POST) without
opening browser UI or Selenium unless explicitly needed as a fallback.

Capabilities:
  1. Parses URLs, form fields, and intent from natural language instructions.
  2. Directly fetches site HTML, captures sessions/cookies, and parses backend forms.
  3. Intelligently maps user parameters (PIN, hall ticket, month, semester, etc.) to form inputs.
  4. Discovers JavaScript hidden values (e.g. mode.value = 'getData').
  5. Posts directly to the backend endpoint (multipart/form-data or urlencoded).
  6. Parses and extracts tabular results, grades, GPAs, and student records into natural speech.
"""

import re
import ssl
import json
import asyncio
import urllib.request
import urllib.parse
from typing import Dict, Any, Tuple, List, Optional
from core.shared.logger import get_logger

logger = get_logger("WebAutomationService")

# SSL context that allows gov / institutional portals with unusual certificate chains
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}


# ─── Natural Language Extraction Helpers ─────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase + collapse whitespace for fuzzy label matching."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _extract_url(text: str) -> Optional[str]:
    """Pull the first http/https URL out of free-form text."""
    m = re.search(r"https?://[^\s]+", text)
    return m.group(0).rstrip(".,;") if m else None


def _extract_fields(text: str) -> Dict[str, str]:
    """
    Extract key=value pairs from natural language.
    Handles PINs, hall tickets, months, semesters, roll numbers, etc.
    """
    # Strip URLs first to avoid URL fragments being parsed as keys
    clean_text = re.sub(r"https?://\S+", "", text).strip()
    fields: Dict[str, str] = {}

    _STOP_WORDS = {
        "a", "an", "the", "this", "that", "my", "of", "and", "for", "to",
        "is", "are", "was", "be", "now", "check", "mine", "portal"
    }

    _FIELD_PATTERNS = [
        (r"\bpin\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",          "pin"),
        (r"\bhtno\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",         "htno"),
        (r"\bhall\s*ticket\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)","hall ticket"),
        (r"\broll\s*no\.?\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)", "roll no"),
        (r"\bregistration\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)", "registration"),
        (r"\breg\s*no\.?\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",  "reg no"),
        (r"\benrollment\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",   "enrollment"),
        (r"\bcandidate\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",    "candidate"),
        (r"\bmonth\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",        "month"),
        (r"\bsemester\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",     "semester"),
        (r"\bsem\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",          "semester"),
        (r"\byear\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",         "year"),
        (r"\bname\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",         "name"),
        (r"\bpassword\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",     "password"),
        (r"\busername\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",     "username"),
        (r"\bdob\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)",          "dob"),
        (r"\bdate\s+of\s+birth\s+(?:is\s+|:?\s*)([A-Za-z0-9\-/]+)", "dob"),
    ]
    for pat, canonical_key in _FIELD_PATTERNS:
        m = re.search(pat, clean_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(".,!?")
            if canonical_key not in fields:
                fields[canonical_key] = val

    # Pattern: "<key> is <value>"
    for m in re.finditer(
        r"(?:^|[\s,;])([a-zA-Z][a-zA-Z\s]{0,20}?)\s+is\s+([A-Za-z0-9\-/]+)",
        clean_text, re.IGNORECASE
    ):
        key = _norm(m.group(1))
        val = m.group(2).strip()
        key_words = key.split()
        if len(key_words) > 3 or all(w in _STOP_WORDS for w in key_words):
            continue
        while key_words and key_words[0] in _STOP_WORDS:
            key_words.pop(0)
        if not key_words:
            continue
        key = " ".join(key_words)
        if key not in fields:
            fields[key] = val

    # Strip ordinal suffixes from values (4th → 4, 2nd → 2)
    for k in list(fields.keys()):
        v = fields[k]
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)$", r"\1", v, flags=re.IGNORECASE)
        if cleaned != v:
            fields[k] = cleaned

    # Deduplicate: if two keys have the same value, keep the shorter key
    val_to_keys: Dict[str, List[str]] = {}
    for k, v in fields.items():
        val_to_keys.setdefault(v, []).append(k)
    deduped = {}
    for v, keys in val_to_keys.items():
        best_key = min(keys, key=lambda k: len(k))
        deduped[best_key] = v

    return deduped


def _field_match_score(label: str, field_key: str) -> int:
    """Fuzzy-matches extracted field keys against form input names, IDs, and labels."""
    label = _norm(label)
    field_key = _norm(field_key)

    if not label or not field_key:
        return 0
    if label == field_key:
        return 100
    if field_key in label:
        return 80
    if label in field_key:
        return 70

    label_tokens = set(label.split())
    key_tokens = set(field_key.split())
    overlap = label_tokens & key_tokens
    if overlap:
        return 50 + len(overlap) * 10

    aliases = {
        "pin": ["pin", "pincode", "aadhar", "aadhar1", "enrollment", "roll", "htno", "hallticket",
                "hall ticket", "registration", "regno", "reg no", "candidate", "candidate no"],
        "month": ["month", "feesmonthyear", "exam month", "month of exam", "examination month"],
        "semester": ["semester", "sem", "grade2", "year", "term"],
        "name": ["name", "candidate name", "student name"],
    }
    for canonical, alts in aliases.items():
        if field_key == canonical or field_key in alts:
            for alt in alts:
                if alt in label:
                    return 65
    return 0


# ─── Form Parser & Form Submitter ─────────────────────────────────────────────

def _extract_forms(html: str, base_url: str) -> List[Dict[str, Any]]:
    """Parse forms, inputs, selects, and submit actions from HTML."""
    forms = []
    form_matches = re.finditer(r'<form\b([^>]*)>([\s\S]*?)</form>', html, re.I)

    for fm in form_matches:
        form_attrs_raw = fm.group(1)
        form_body = fm.group(2)

        def get_attr(attr: str, text: str) -> str:
            m = re.search(rf'{attr}=[\'"]([^\'"]*)[\'"]', text, re.I)
            return m.group(1) if m else ""

        action = get_attr("action", form_attrs_raw) or base_url
        if not action.startswith("http"):
            action = urllib.parse.urljoin(base_url, action)

        method = (get_attr("method", form_attrs_raw) or "POST").upper()
        enctype = get_attr("enctype", form_attrs_raw) or "application/x-www-form-urlencoded"

        # Parse inputs
        inputs = []
        for im in re.finditer(r'<input\b([^>]*)>', form_body, re.I):
            raw = im.group(1)
            inp_type = (get_attr("type", raw) or "text").lower()
            inp_name = get_attr("name", raw)
            inp_id = get_attr("id", raw)
            inp_val = get_attr("value", raw)

            # Find preceding label or text
            start_pos = max(0, im.start() - 200)
            context_text = re.sub(r'<[^>]+>', ' ', form_body[start_pos:im.start()]).strip()

            inputs.append({
                "type": inp_type,
                "name": inp_name,
                "id": inp_id,
                "value": inp_val,
                "context": context_text
            })

        # Parse selects
        selects = []
        for sm in re.finditer(r'<select\b([^>]*)>([\s\S]*?)</select>', form_body, re.I):
            s_raw = sm.group(1)
            s_body = sm.group(2)
            s_name = get_attr("name", s_raw)
            s_id = get_attr("id", s_raw)

            # Find preceding label
            start_pos = max(0, sm.start() - 200)
            context_text = re.sub(r'<[^>]+>', ' ', form_body[start_pos:sm.start()]).strip()

            options = []
            for om in re.finditer(r'<option\b([^>]*)>([\s\S]*?)</option>', s_body, re.I):
                o_val = get_attr("value", om.group(1))
                o_text = re.sub(r'<[^>]+>', '', om.group(2)).strip()
                options.append({"value": o_val, "text": o_text})

            selects.append({
                "name": s_name,
                "id": s_id,
                "options": options,
                "context": context_text
            })

        # Find submit button onclick handler to determine active submit mode
        clean_html = re.sub(r'/\*[\s\S]*?\*/', '', html)
        clean_html = re.sub(r'//[^\n]*', '', clean_html)

        js_mode = None
        submit_btn_matches = re.finditer(r'<(?:input|button)\b[^>]*onclick=[\'"]([^\'"]+)[\'"][^>]*>', form_body, re.I)
        for sbm in submit_btn_matches:
            fn_match = re.search(r'(\w+)\s*\(', sbm.group(1))
            if fn_match:
                fn_name = fn_match.group(1)
                fn_def_match = re.search(rf'function\s+{fn_name}\s*\([^)]*\)\s*\{{([\s\S]*?)\}}', clean_html, re.I)
                if fn_def_match:
                    fn_body = fn_def_match.group(1)
                    mode_m = re.search(r'mode\.value\s*=\s*[\'"](\w+)[\'"]', fn_body, re.I)
                    if mode_m:
                        js_mode = mode_m.group(1)
                        break

        if not js_mode:
            for preferred in ["getData", "view", "search", "query", "submit"]:
                if re.search(rf'mode\.value\s*=\s*[\'"]{preferred}[\'"]', clean_html, re.I):
                    js_mode = preferred
                    break
        if not js_mode:
            js_match = re.search(r'mode\.value\s*=\s*[\'"](\w+)[\'"]', clean_html, re.I)
            if js_match:
                js_mode = js_match.group(1)

        forms.append({
            "action": action,
            "method": method,
            "enctype": enctype,
            "inputs": inputs,
            "selects": selects,
            "js_mode": js_mode
        })

    return forms


def _match_form_data(form: Dict[str, Any], user_fields: Dict[str, str]) -> Dict[str, str]:
    """Map extracted user fields to form inputs and selects, including hidden values."""
    payload: Dict[str, str] = {}

    # 1. Fill all default hidden fields
    for inp in form["inputs"]:
        if inp["name"]:
            if inp["type"] == "hidden":
                payload[inp["name"]] = inp["value"]

    # 2. If JS sets a mode (e.g. "getData"), apply it
    if form.get("js_mode"):
        payload["mode"] = form["js_mode"]

    # 3. Match user fields to text inputs
    for field_key, field_value in user_fields.items():
        best_score = 0
        best_input_name = None

        for inp in form["inputs"]:
            if inp["type"] in ("hidden", "submit", "button", "reset"):
                continue
            name = inp["name"] or ""
            inp_id = inp["id"] or ""
            context = inp.get("context", "")

            candidates = [name, inp_id, context]
            for cand in candidates:
                score = _field_match_score(cand, field_key)
                if score > best_score:
                    best_score = score
                    best_input_name = name

        if best_input_name and best_score >= 50:
            # Capitalize PIN / alphanumeric codes if appropriate
            val_to_send = field_value.upper() if ("pin" in field_key or "htno" in field_key) else field_value
            payload[best_input_name] = val_to_send
            logger.info(f"Matched input '{best_input_name}' <- {field_key}={val_to_send} (score {best_score})")

    # 4. Match user fields to selects and choose the best option
    for field_key, field_value in user_fields.items():
        best_score = 0
        best_select = None

        for sel in form["selects"]:
            candidates = [sel["name"], sel["id"], sel.get("context", "")]
            for cand in candidates:
                score = _field_match_score(cand, field_key)
                if score > best_score:
                    best_score = score
                    best_select = sel

        if best_select and best_score >= 50:
            # Find best matching option
            chosen_opt_val = None
            f_val_norm = _norm(field_value)

            # Try exact match on option value or text
            for opt in best_select["options"]:
                o_val_norm = _norm(opt["value"])
                o_text_norm = _norm(opt["text"])
                if f_val_norm == o_val_norm or f_val_norm == o_text_norm:
                    chosen_opt_val = opt["value"]
                    break

            # Try partial substring match (e.g. "4" in "4SEM", "MA26" in "MA26")
            if not chosen_opt_val:
                for opt in best_select["options"]:
                    o_val_norm = _norm(opt["value"])
                    o_text_norm = _norm(opt["text"])
                    if f_val_norm in o_val_norm or f_val_norm in o_text_norm or o_val_norm in f_val_norm:
                        chosen_opt_val = opt["value"]
                        break

            if chosen_opt_val:
                payload[best_select["name"]] = chosen_opt_val
                logger.info(f"Matched select '{best_select['name']}' <- {field_key}={chosen_opt_val}")

    return payload


def _build_multipart_payload(payload: Dict[str, str], boundary: str) -> bytes:
    """Encode dictionary into multipart/form-data bytes."""
    lines = []
    for k, v in payload.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    lines.append(f"--{boundary}--\r\n")
    return "".join(lines).encode("utf-8")


# ─── Results Parser ───────────────────────────────────────────────────────────

def _parse_results(html_text: str, user_fields: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
    """
    Intelligently parse the backend response HTML.
    Extracts student results, subject breakdowns, grand totals, and GPA.
    """
    tables = re.findall(r'<table[\s\S]*?</table>', html_text, re.I)

    # 1. Check for tabular results (e.g. SBTET or university result marksheet)
    if tables:
        for t in tables:
            rows = re.findall(r'<tr[\s\S]*?</tr>', t, re.I)
            parsed_rows = []
            for r in rows:
                cells = re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.I)
                clean_cells = [' '.join(re.sub(r'<[^>]+>', ' ', c).split()) for c in cells]
                clean_cells = [c for c in clean_cells if c]
                if clean_cells:
                    parsed_rows.append(clean_cells)

            data: Dict[str, Any] = {"subjects": []}
            for row in parsed_rows:
                if len(row) >= 2:
                    k_lower = row[0].lower().strip()
                    if "pin" in k_lower or "reg" in k_lower or "roll" in k_lower:
                        data["pin"] = row[1]
                    elif "name" in k_lower and "branch" not in k_lower:
                        data["name"] = row[1]
                    elif "branch" in k_lower or "course" in k_lower:
                        data["branch"] = row[1]
                    elif "grand total" in k_lower or "total marks" in k_lower:
                        data["grand_total"] = row[1]
                    elif "gpa" in k_lower or "sgpa" in k_lower or "cgpa" in k_lower:
                        data["gpa"] = row[1]
                    elif "result" in k_lower or "status" in k_lower:
                        data["result"] = row[1]
                    elif "month" in k_lower or "year" in k_lower:
                        data["month_year"] = row[1]

                # Check for subject rows (starts with course/paper number e.g. 401, 101, CS101)
                _HEADER_WORDS = {"paper", "subject", "code", "sl", "sno", "s.no", "course", "name"}
                if len(row) >= 6 and re.match(r'^[A-Za-z0-9\-]{3,8}$', row[0]) and row[0].lower() not in _HEADER_WORDS:
                    subj = {
                        "paper": row[0],
                        "total": row[3] if len(row) > 3 else "",
                        "grade": row[6] if len(row) > 6 else (row[4] if len(row) > 4 else ""),
                        "status": row[7] if len(row) > 7 else (row[-1] if len(row) > 0 else "")
                    }
                    data["subjects"].append(subj)

            # If we found student or subject data, format a crisp, professional spoken summary
            if data.get("name") or data.get("pin") or data.get("result") or data.get("subjects"):
                summary_parts = []
                name = data.get("name", "Student")
                pin = data.get("pin", user_fields.get("pin", ""))
                branch = data.get("branch", "")
                result = data.get("result", "")
                gpa = data.get("gpa", "")
                total = data.get("grand_total", "")

                summary_parts.append(f"Here are the diploma results for {name}")
                if pin:
                    summary_parts.append(f"with PIN {pin}")
                if branch:
                    summary_parts.append(f"in {branch}.")

                if result:
                    res_line = f"Overall Result is {result}"
                    if gpa:
                        res_line += f" with a GPA of {gpa}"
                    if total:
                        res_line += f" and a Grand Total of {total}"
                    summary_parts.append(res_line + ".")

                subs = data.get("subjects", [])
                if subs:
                    passed = sum(1 for s in subs if s.get("status", "").upper() in ["P", "PASS", "PASSED"])
                    summary_parts.append(f"Passed {passed} out of {len(subs)} subjects.")
                    # Highlight top scores
                    highlights = [f"Paper {s['paper']}: {s['total']} marks (Grade {s['grade']})" for s in subs[:4] if s.get('total') and s.get('total') != 'Total']
                    if highlights:
                        summary_parts.append("Scores: " + ", ".join(highlights) + ("..." if len(subs) > 4 else "."))

                return " ".join(summary_parts), data

    # 2. Check for JSON response
    try:
        json_data = json.loads(html_text)
        return f"Backend responded with data: {json.dumps(json_data)[:300]}", json_data
    except Exception:
        pass

    # 3. Fallback: Clean text extraction
    clean_text = re.sub(r'<[^>]+>', ' ', html_text)
    clean_lines = [l.strip() for l in clean_text.splitlines() if l.strip()]

    result_keywords = ["result", "grade", "marks", "pass", "fail", "total", "score", "status", "gpa"]
    relevant = [l for l in clean_lines if any(k in l.lower() for k in result_keywords)]

    if relevant:
        snippet = " | ".join(relevant[:10])
        return f"Here is what the portal returned: {snippet}", {"raw_lines": relevant}

    return "The portal responded successfully, but no grade or result table was found on the page.", {}


# ─── Main Service ──────────────────────────────────────────────────────────────

class WebAutomationService:
    """
    Intelligent site operations for AIVA.
    Directly communicates with backends via HTTP (no UI / browser needed).
    """

    def __init__(self):
        logger.info("WebAutomationService initialized (Direct Backend Engine)")

    async def execute(self, instruction: str) -> Dict[str, Any]:
        """
        Main entry point. Asynchronously executes direct backend communication.
        """
        return await asyncio.to_thread(self._execute_sync, instruction)

    def _execute_sync(self, instruction: str) -> Dict[str, Any]:
        url = _extract_url(instruction)
        if not url:
            return {
                "status": "error",
                "summary": "I couldn't find a website link in your request. Please include the portal URL.",
                "raw_text": "",
                "url": ""
            }

        user_fields = _extract_fields(instruction)
        logger.info(f"Target URL: {url}")
        logger.info(f"Extracted fields: {user_fields}")

        try:
            # ───────────────────────────────────────────────────────────
            # STEP 1: Direct HTTP GET (Capture Cookies, Forms, Action)
            # ───────────────────────────────────────────────────────────
            get_req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
            res = urllib.request.urlopen(get_req, context=_SSL_CTX, timeout=15)

            # Capture cookies (e.g. JSESSIONID)
            cookies_header = res.headers.get_all("Set-Cookie") or []
            cookie_tokens = [c.split(";")[0] for c in cookies_header]
            cookie_str = "; ".join(cookie_tokens)

            page_html = res.read().decode("utf-8", errors="ignore")
            logger.info(f"Initial GET returned 200 (length: {len(page_html)} bytes, cookies: {cookie_str})")

            # ───────────────────────────────────────────────────────────
            # STEP 2: Parse Forms & Determine Backend Action
            # ───────────────────────────────────────────────────────────
            forms = _extract_forms(page_html, url)
            if not forms:
                logger.info("No forms found on page, checking content directly")
                summary, data = _parse_results(page_html, user_fields)
                return {
                    "status": "success",
                    "summary": summary,
                    "raw_text": page_html[:2000],
                    "url": url,
                    "data": data
                }

            # Choose the primary form
            target_form = forms[0]
            action_url = target_form["action"]
            method = target_form["method"]
            enctype = target_form["enctype"]

            # Map user fields (PIN, month, sem) to form inputs and selects
            payload = _match_form_data(target_form, user_fields)
            logger.info(f"Submitting to {action_url} ({method}, {enctype}) with payload: {payload}")

            # ───────────────────────────────────────────────────────────
            # STEP 3: Execute Direct HTTP Request to Backend
            # ───────────────────────────────────────────────────────────
            req_headers = dict(_DEFAULT_HEADERS)
            req_headers["Referer"] = url
            if cookie_str:
                req_headers["Cookie"] = cookie_str

            parsed_action = urllib.parse.urlparse(action_url)
            req_headers["Origin"] = f"{parsed_action.scheme}://{parsed_action.netloc}"

            if method == "POST":
                if "multipart/form-data" in enctype.lower():
                    boundary = "----WebKitFormBoundary" + "7MA4YWxkTrZu0gW"
                    body_bytes = _build_multipart_payload(payload, boundary)
                    req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                else:
                    body_bytes = urllib.parse.urlencode(payload).encode("utf-8")
                    req_headers["Content-Type"] = "application/x-www-form-urlencoded"

                post_req = urllib.request.Request(action_url, data=body_bytes, headers=req_headers)
                post_res = urllib.request.urlopen(post_req, context=_SSL_CTX, timeout=20)
                result_html = post_res.read().decode("utf-8", errors="ignore")
            else:
                query_str = urllib.parse.urlencode(payload)
                full_url = f"{action_url}?{query_str}" if query_str else action_url
                get_sub_req = urllib.request.Request(full_url, headers=req_headers)
                sub_res = urllib.request.urlopen(get_sub_req, context=_SSL_CTX, timeout=20)
                result_html = sub_res.read().decode("utf-8", errors="ignore")

            logger.info(f"Backend submission returned response (length: {len(result_html)} bytes)")

            # ───────────────────────────────────────────────────────────
            # STEP 4: Parse & Summarize Result
            # ───────────────────────────────────────────────────────────
            summary, data = _parse_results(result_html, user_fields)

            return {
                "status": "success",
                "summary": summary,
                "raw_text": result_html[:3000],
                "url": action_url,
                "data": data,
                "filled_fields": payload
            }

        except Exception as e:
            logger.error(f"Backend communication error: {e}", exc_info=True)
            return {
                "status": "error",
                "summary": f"I couldn't reach the portal backend: {e}",
                "raw_text": "",
                "url": url
            }

    async def web_automation_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Entry point called by kernel capability dispatcher."""
        instruction = (
            action.get("instruction")
            or action.get("parameters", {}).get("instruction")
            or action.get("parameters", {}).get("goal")
            or action.get("goal", "")
        )
        if not instruction:
            return {"status": "error", "summary": "No instruction provided for web operation."}
        return await self.execute(instruction)
