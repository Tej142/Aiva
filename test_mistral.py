from mistralai import Mistral

API_KEY = "9D4i1LFH9umnl1SM6gwktuC15JhGZeYW"

client = Mistral(
    api_key=API_KEY
)

response = client.chat.complete(

    model="mistral-small-latest",

    messages=[
        {
            "role": "user",
            "content": "Say hello from Mistral"
        }
    ]
)

print(
    response
    .choices[0]
    .message
    .content
)