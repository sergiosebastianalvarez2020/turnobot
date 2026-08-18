import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

print("1. API KEY encontrada:", bool(api_key))

client = genai.Client(api_key=api_key)

print("2. Cliente Gemini creado")

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents="Respondé solamente: GEMINI FUNCIONA"
)

print("3. Respuesta recibida:")
print(response.text)