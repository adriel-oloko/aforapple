import requests

with open("D:\Downloads\donaldtrump.m4a", "rb") as audio:
    response = requests.post(
        "https://api.fish.audio/v1/asr",
        headers={"Authorization": "Bearer 9fba1a9dbf1c42c49b9b2624e9c15644"},
        files={"audio": audio},
        data={"language": "en"},
    )

print(response.json())#["text"])
