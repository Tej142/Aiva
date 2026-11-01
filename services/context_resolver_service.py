class ContextResolverService:

    def __init__(
        self,
        conversation_memory_service
    ):

        self.conversation_memory_service = (
            conversation_memory_service
        )

    # =====================================
    # Resolve Query Context
    # =====================================

    def resolve(
        self,
        query
    ):

        if not query:

            return query

        current_topic = (

            self.conversation_memory_service
            .get_current_topic()

        )

        if not current_topic:

            return query

        resolved_query = query

        # =====================================
        # Context Reference Words
        # =====================================

        reference_words = [

            "he",
            "him",
            "his",
            "she",
            "her",
            "hers",
            "it",
            "its"

        ]

        words = query.split()

        resolved_words = []

        context_used = False

        for word in words:

            clean_word = (

                word
                .lower()
                .strip(
                    ".,?!"
                )

            )

            if clean_word in reference_words:

                punctuation = ""

                if word[-1:] in ".,?!":

                    punctuation = word[-1]

                resolved_words.append(

                    current_topic
                    + punctuation

                )

                context_used = True

            else:

                resolved_words.append(
                    word
                )

        resolved_query = " ".join(
            resolved_words
        )

        if context_used:

            print(
                "[ContextResolverService] "
                f"Original: {query}"
            )

            print(
                "[ContextResolverService] "
                f"Resolved: {resolved_query}"
            )

        return resolved_query