class ChatRuntime:

    def __init__(
        self,
        input_handler,
        kernel
    ):

        self.input_handler = (
            input_handler
        )

        self.kernel = (
            kernel
        )

        self.running = False


    async def start(self):

        self.running = True

        print(
            "\n=============================="
        )

        print(
            "AIVA CHAT RUNTIME"
        )

        print(
            "Type 'exit' to stop AIVA."
        )

        print(
            "==============================\n"
        )


        while (
            self.running
            and
            self.kernel.running
        ):

            try:

                user_input = await (
                    self.get_user_input()
                )


                if not user_input.strip():
                    continue

                if user_input.strip().lower() in {"/allvoice", "toggle allvoice"}:
                    if getattr(self.kernel, "wake_word_service", None):
                        if self.kernel.wake_word_service.running and self.kernel.wake_word_service.continuous_mode:
                            self.kernel.stop_voice_mode()
                            print("\n🎙️ [AIVA]: Continuous all-voice mode DISABLED.\n")
                        else:
                            self.kernel.start_voice_mode(continuous=True)
                            print("\n🎙️ [AIVA]: Continuous All-Voice Mode ENABLED! Speak freely without saying 'Hey AIVA'.\n")
                    continue

                if user_input.strip().lower() in {"/voice", "/mic", "toggle voice"}:
                    if getattr(self.kernel, "wake_word_service", None):
                        if self.kernel.wake_word_service.running and not self.kernel.wake_word_service.continuous_mode:
                            self.kernel.stop_voice_mode()
                            print("\n🎙️ [AIVA]: Voice mode DISABLED.\n")
                        else:
                            self.kernel.start_voice_mode(continuous=False)
                            print("\n🎙️ [AIVA]: Hands-free voice mode ENABLED! Say 'Hey AIVA' to speak.\n")
                    continue


                should_continue = await (

                    self.input_handler
                    .process_input(
                        user_input
                    )

                )


                if not should_continue:

                    self.running = False

                    print(
                        "\n[AIVA] Ending conversation..."
                    )

                    await (
                        self.kernel
                        .shutdown()
                    )

                    break

                import asyncio
                await asyncio.sleep(0.4)
                while not self.kernel.task_queue.empty():
                    await asyncio.sleep(0.3)


            except KeyboardInterrupt:

                self.running = False

                print(
                    "\n[AIVA] Interrupted by user."
                )

                await (
                    self.kernel
                    .shutdown()
                )

                break


            except Exception as error:

                print(
                    "[ChatRuntime Error]",
                    error
                )


    async def get_user_input(self):

        import asyncio

        loop = (
            asyncio
            .get_running_loop()
        )

        user_input = await (
            loop.run_in_executor(
                None,
                input,
                "\nYou: "
            )
        )

        return user_input