class ReasoningService:

    def __init__(
        self,
        mistral_service
    ):

        self.mistral_service = (
            mistral_service
        )

    async def answer_from_memory(
        self,
        user_query,
        memory
    ):

        answer = await (

            self.mistral_service
            .answer_from_memory(

                user_query,

                memory

            )

        )

        return answer