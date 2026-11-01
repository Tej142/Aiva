import re


class MemoryExtractionService:

    def __init__(self):

        self.incomplete_patterns = [

            (
                r"remember (?:that )?my name",
                "name",
                "Sure. What is your name?"
            ),

            (
                r"remember (?:that )?what i like",
                "likes",
                "Sure. What do you like?"
            ),

            (
                r"remember (?:that )?my preference",
                "preference",
                "Sure. What preference should I remember?"
            )

        ]


        self.complete_patterns = [

            (
                r"remember (?:that )?my name is (.+)",
                "name"
            ),

            (
                r"remember (?:that )?i am called (.+)",
                "name"
            ),

            (
                r"remember (?:that )?you can call me (.+)",
                "name"
            ),

            (
                r"remember (?:that )?i like (.+)",
                "likes"
            ),

            (
                r"remember (?:that )?i love (.+)",
                "likes"
            ),

            (
                r"remember (?:that )?i prefer (.+)",
                "preference"
            )

        ]


    def extract(
        self,
        user_input
    ):

        text = (
            user_input
            .strip()
        )


        # =====================================
        # COMPLETE MEMORY STATEMENTS
        # =====================================

        for pattern, key in self.complete_patterns:

            match = re.fullmatch(

                pattern,

                text,

                flags=re.IGNORECASE

            )


            if match:

                value = (

                    match
                    .group(1)
                    .strip()
                    .rstrip(".!?")

                )


                if not value:

                    return None


                return {

                    "status": "complete",

                    "key": key,

                    "value": value

                }


        # =====================================
        # INCOMPLETE MEMORY REQUESTS
        # =====================================

        for (
            pattern,
            key,
            question
        ) in self.incomplete_patterns:

            match = re.fullmatch(

                pattern,

                text,

                flags=re.IGNORECASE

            )


            if match:

                return {

                    "status": "incomplete",

                    "key": key,

                    "question": question

                }


        # =====================================
        # GENERIC EXPLICIT FACT
        # =====================================

        generic_match = re.fullmatch(

            r"remember (?:that )?(.+)",

            text,

            flags=re.IGNORECASE

        )


        if generic_match:

            value = (

                generic_match
                .group(1)
                .strip()
                .rstrip(".!?")

            )


            # Prevent incomplete phrases
            incomplete_values = {

                "my name",
                "my preference",
                "what i like",
                "something",
                "this"

            }


            if value.lower() in incomplete_values:

                return None


            return {

                "status": "complete",

                "key": "important_fact",

                "value": value

            }


        return None