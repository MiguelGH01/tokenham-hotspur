"""judge.eval.factory target for server/evals scenarios.

DISCLAIMER: this judges every scenario with whatever LLM bot.py's own
build_llm() would construct for a live call — i.e. LLM_PROVIDER (helmcode by
default; gemini/openai are the other options bot.py supports), not a model
pinned specifically for judging. Switching LLM_PROVIDER before a run changes
the judge too, so eval results are only comparable across runs made with the
same LLM_PROVIDER.
"""

from dotenv import load_dotenv
from pipecat.services.llm_service import LLMService

from bot import build_llm

# The eval CLI runs as its own process (not bot.py), so it never loads .env on its own.
load_dotenv()


def helmcode_judge(config: dict) -> LLMService:
    """judge.eval.factory target: judge scenarios with the bot's own configured LLM."""
    return build_llm()
