import os
import re
import json
import time
import urllib.parse
from core.shared.config_loader import get_path
from core.shared.logger import get_logger

logger = get_logger("TaskPatternService")


class TaskPatternService:
    def __init__(self, memory_file: str = "memory/task_patterns.json"):
        self.memory_file = memory_file
        self.patterns = self._load_patterns()

    def _load_patterns(self) -> dict:
        """Load learned task patterns from disk."""
        if not os.path.exists(self.memory_file):
            return {}
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load task patterns: {e}")
            return {}

    def _save_patterns(self):
        """Save task patterns to disk."""
        os.makedirs(os.path.dirname(self.memory_file) or ".", exist_ok=True)
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.patterns, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save task patterns: {e}")

    def learn_pattern(self, goal: str, workflow_data) -> str:
        """Analyzes a successful goal and workflow, extracts generalized templates, and stores the pattern."""
        if not goal or not workflow_data:
            return ""

        if isinstance(workflow_data, list):
            workflow_nodes = workflow_data
        elif isinstance(workflow_data, dict):
            workflow_nodes = workflow_data.get("workflow", [])
        else:
            return ""

        if not workflow_nodes:
            return ""

        clean_goal = goal.strip().lower()

        # Check if identical pattern or template already exists
        template_id, template_goal, slots, template_nodes = self._generalize_workflow(clean_goal, workflow_nodes)

        if template_id in self.patterns:
            self.patterns[template_id]["frequency"] = self.patterns[template_id].get("frequency", 1) + 1
            self.patterns[template_id]["last_executed"] = time.time()
            self.patterns[template_id]["examples"] = list(set(self.patterns[template_id].get("examples", []) + [clean_goal]))
            logger.info(f"Updated existing task pattern: '{template_id}' (freq={self.patterns[template_id]['frequency']})")
        else:
            self.patterns[template_id] = {
                "id": template_id,
                "template_goal": template_goal,
                "slots": slots,
                "workflow_template": template_nodes,
                "frequency": 1,
                "created_at": time.time(),
                "last_executed": time.time(),
                "examples": [clean_goal]
            }
            logger.info(f"Learned NEW task pattern: '{template_id}'")

        self._save_patterns()
        return template_id

    def match_pattern(self, goal: str) -> dict | None:
        """Matches a user goal against learned patterns and returns instantiated workflow nodes if matched."""
        if not goal:
            return None

        clean_goal = goal.strip().lower()

        compound_action_pattern = re.compile(
            r"\b(?:and\s+(?:note|create|write|open|launch|play|generate|search|take|mute|set)|then\s+|also\s+)\b",
            re.IGNORECASE
        )

        # 1. Exact example match
        for pid, pdata in self.patterns.items():
            if clean_goal in pdata.get("examples", []):
                template = pdata.get("template_goal", "")
                slots = pdata.get("slots", [])
                extracted_values = {}
                if slots:
                    regex_str, _ = self._build_regex_from_template(template, slots)
                    match = re.match(regex_str, clean_goal, re.IGNORECASE)
                    if match:
                        extracted_values = match.groupdict()

                logger.info(f"Instant exact match for task pattern '{pid}' with values: {extracted_values}")
                instantiated = self._instantiate_workflow(pdata["workflow_template"], extracted_values, fallback_text=clean_goal)
                pdata["frequency"] += 1
                pdata["last_executed"] = time.time()
                self._save_patterns()
                return {"workflow": instantiated, "pattern_id": pid, "source": "learned_pattern"}

        # 2. Template / Slot regex match
        for pid, pdata in self.patterns.items():
            template = pdata.get("template_goal", "")
            slots = pdata.get("slots", [])
            if not slots:
                continue

            regex_str, slot_names = self._build_regex_from_template(template, slots)
            match = re.match(regex_str, clean_goal, re.IGNORECASE)
            if match:
                extracted_values = match.groupdict()

                # Boundary Check: Ensure a slot didn't greedily swallow a secondary action command
                invalid_swallow = False
                for sname, sval in extracted_values.items():
                    if compound_action_pattern.search(sval) and not any(action_w in template for action_w in ["note", "create", "word"]):
                        invalid_swallow = True
                        break

                if invalid_swallow:
                    continue

                logger.info(f"Fuzzy template match for pattern '{pid}' with slots: {extracted_values}")

                # Instantiate workflow template with extracted values
                instantiated_workflow = self._instantiate_workflow(pdata["workflow_template"], extracted_values, fallback_text=clean_goal)

                pdata["frequency"] += 1
                pdata["last_executed"] = time.time()
                self._save_patterns()
                return {"workflow": instantiated_workflow, "pattern_id": pid, "source": "learned_pattern"}

        return None

    def _generalize_workflow(self, goal: str, nodes: list) -> tuple[str, str, list, list]:
        """Generalizes string parameters (e.g. query strings, file names) into slot variables."""
        template_nodes = json.loads(json.dumps(nodes))
        slots = []
        template_goal = goal

        # Common extraction heuristics
        # 1. YouTube / Google Search patterns
        search_match = re.search(r"(?:search for|search|play|find|about)\s+([a-zA-Z0-9\s]+?)(?:\s+in\s+youtube|\s+on\s+youtube|\s+in\s+chrome|\s+in\s+browser|$)", goal)
        if search_match:
            val = search_match.group(1).strip()
            if val and len(val) > 1 and val not in {"chrome", "youtube", "browser", "google"}:
                slots.append("query")
                template_goal = template_goal.replace(val, "{query}")
                # Replace query in node parameters
                for n in template_nodes:
                    params = n.get("parameters", {})
                    if "url" in params and isinstance(params["url"], str):
                        params["url"] = re.sub(r"(search_query=)[^&]+", r"\g<1>{query_encoded}", params["url"])
                        params["url"] = re.sub(r"(q=)[^&]+", r"\g<1>{query_encoded}", params["url"])
                    if "topic" in params and params["topic"].lower() == val.lower():
                        params["topic"] = "{query}"
                    if "query" in params and params["query"].lower() == val.lower():
                        params["query"] = "{query}"

        # 2. Document creation patterns
        doc_match = re.search(r"(?:notes|doc|document)\s+(?:about|on)\s+([a-zA-Z0-9\s]+?)(?:\s+in\s+that|\s+and|$)", goal)
        if doc_match and "query" not in slots:
            topic_val = doc_match.group(1).strip()
            if topic_val:
                slots.append("topic")
                template_goal = template_goal.replace(topic_val, "{topic}")
                for n in template_nodes:
                    params = n.get("parameters", {})
                    if "topic" in params and params["topic"].lower() == topic_val.lower():
                        params["topic"] = "{topic}"

        # Clean template id
        clean_slug = re.sub(r"[^a-zA-Z0-9_]+", "_", template_goal).strip("_")
        pattern_id = f"pattern_{clean_slug[:40]}"

        return pattern_id, template_goal, slots, template_nodes

    def _build_regex_from_template(self, template: str, slots: list) -> tuple[str, list]:
        """Converts '{query}' template into a compiled regex string."""
        escaped = re.escape(template)
        for s in slots:
            escaped = escaped.replace(re.escape(f"{{{s}}}"), f"(?P<{s}>.+?)")
        return f"^{escaped}$", slots

    def _instantiate_workflow(self, template_nodes: list, values: dict, fallback_text: str = "") -> list:
        """Substitutes extracted slot values into the workflow node parameters."""
        instantiated = json.loads(json.dumps(template_nodes))
        query_val = values.get("query", values.get("topic", ""))

        if not query_val and fallback_text:
            # Fallback extraction from goal if slot was not explicitly parsed
            search_m = re.search(r"(?:search for|search|about|find)\s+([a-zA-Z0-9\s]+?)(?:\s+in|\s+on|\s+and|$)", fallback_text, re.IGNORECASE)
            if search_m:
                query_val = search_m.group(1).strip()

        query_encoded = urllib.parse.quote_plus(query_val) if query_val else ""

        for n in instantiated:
            params = n.get("parameters", {})
            for k, v in params.items():
                if isinstance(v, str):
                    for slot_k, slot_v in values.items():
                        v = v.replace(f"{{{slot_k}}}", slot_v)
                    v = v.replace("{query_encoded}", query_encoded)
                    v = v.replace("{query}", query_val)
                    v = v.replace("{topic}", query_val)
                    # Clean any trailing placeholder residue
                    v = re.sub(r"\{[a-zA-Z0-9_]+\}", query_val, v)
                    params[k] = v

        return instantiated

    def list_learned_patterns(self) -> list:
        """Returns summary of all learned patterns."""
        return [
            {
                "id": pid,
                "template": pdata.get("template_goal"),
                "frequency": pdata.get("frequency", 1),
                "examples_count": len(pdata.get("examples", []))
            }
            for pid, pdata in self.patterns.items()
        ]
