import os

from dotenv import load_dotenv
from pipecat.services.openai.llm import OpenAILLMService

# The eval CLI is a separate process from bot.py, so it never loads .env on its own.
load_dotenv()


def helmcode_judge(config: dict) -> OpenAILLMService:
    """judge.eval.factory target: judge scenarios with the bot's own Helmcode gateway."""
    return OpenAILLMService(
        api_key=os.environ["HELMCODE_API_KEY"],
        base_url=os.getenv("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
        settings=OpenAILLMService.Settings(
            model=config.get("model") or os.getenv("HELMCODE_MODEL", "deepseek-v4-flash")
        ),
    )
