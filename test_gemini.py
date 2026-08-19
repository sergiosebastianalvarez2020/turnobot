import os
import unittest

from dotenv import load_dotenv
from google import genai

load_dotenv()

class TestGeminiIntegration(unittest.TestCase):
    @unittest.skipUnless(os.getenv("GEMINI_API_KEY"), "requiere GEMINI_API_KEY")
    def test_gemini_responde(self):
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents="Respondé solamente: GEMINI FUNCIONA",
        )
        self.assertTrue(response.text.strip())