from services.recall_service import RecallService


recall = RecallService()

result = recall.recall(
    "Tell me about Pythagoras"
)

print(result)