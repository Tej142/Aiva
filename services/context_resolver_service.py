class ContextResolverService:
    """Resolve ambiguous pronoun references using the active user's recent MemoryEngine entities."""

    def __init__(self, memory_engine=None):
        self.memory_engine = memory_engine
        self._last_topics = {}

    def set_current_topic(self, topic: str, user_id: str = "default"):
        if topic:
            self._last_topics[user_id] = topic

    def resolve(self, query: str, user_id: str = None) -> str:
        if not query:
            return query

        uid = user_id or "default"
        current_topic = self._last_topics.get(uid)
        if not current_topic and self.memory_engine and user_id:
            try:
                items = self.memory_engine.store.list_memories(user_id, limit=5)
                for item in items:
                    if item.entities:
                        current_topic = item.entities[0]
                        break
            except Exception:
                pass

        if not current_topic:
            return query

        reference_words = {"he", "him", "his", "she", "her", "hers", "it", "its"}
        resolved_words = []
        context_used = False
        for word in query.split():
            clean_word = word.lower().strip(".,?!")
            if clean_word in reference_words:
                punct = word[-1] if word[-1:] in ".,?!" else ""
                resolved_words.append(current_topic + punct)
                context_used = True
            else:
                resolved_words.append(word)

        resolved_query = " ".join(resolved_words)
        if context_used:
            print(f"[ContextResolverService] Original: {query}")
            print(f"[ContextResolverService] Resolved: {resolved_query}")
        return resolved_query
