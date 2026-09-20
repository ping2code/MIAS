from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

response = client.responses.create(
    model="gpt-5.6",
    input=(
        "Analyze this market headline in one short sentence: "
        "NVIDIA announces a new AI infrastructure partnership."
    ),
)

print(response.output_text)
