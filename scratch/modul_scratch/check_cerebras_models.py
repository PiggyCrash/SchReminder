import os
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path="c:/Work/schreminder/.env")
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL", "https://api.cerebras.ai/v1")

headers = {
    "Authorization": f"Bearer {api_key}"
}
url = f"{base_url.rstrip('/')}/models"
try:
    response = requests.get(url, headers=headers)
    print("Status:", response.status_code)
    if response.ok:
        print(response.json())
    else:
        print(response.text)
except Exception as e:
    print("Error:", e)
