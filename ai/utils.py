"""Backward-compatible shim re-exporting legacy model helpers.

New code should import from ``ai.model_clients`` directly. This module is kept
only to satisfy old imports until all callers are migrated.
"""

from .model_clients.config import (
    CLIENT_ID,
    DEEPSEEK_API_TOKEN,
    GROQ_TOKEN,
    HF_TOKEN,
    MIST_TOKEN,
    SC_TOKEN,
    SECRET,
    BOT_POOL_URL,
    proxies,
)
from .model_clients.history import conversation_history as hist
from .model_clients.gigachat import get_gigachat_token, send_prompt_async
from .model_clients.sambanova import (
    ask_DeepSeek_R1_async,
    ask_DeepSeek_R1_Distill_Llama_70B_async,
    ask_DeepSeek_V3_1_async,
    ask_DeepSeek_V3_1_cb_async,
    ask_DeepSeek_V3_2_async,
    ask_Gemma_3_12b_it_async,
    ask_Gpt_oss_120b_async,
    ask_Llama_4_Maverick_17B_128E_Instruct_async,
    ask_Meta_Llama_3_1_70B_Instruct_async,
    ask_Meta_Llama_3_3_70B_Instruct_async,
    ask_MiniMax_M2_5_async,
    ask_MiniMax_M2_7_async,
    ask_Mixtral_8x22b_async,
)
from .model_clients.huggingface import ask_Gemma_7b_async, ask_Mistral_Nemo_Instruct_async
from .model_clients.web_deepseek import (
    ask_Web_DeepSeek_async,
    ask_Web_DeepSeek_Thinking_async,
    _post_to_bot_pool,
)
from .model_clients.exceptions import extract_choice_content

# Legacy unused global kept at zero to avoid breaking old references.
timeout = 0

__all__ = [
    "CLIENT_ID",
    "DEEPSEEK_API_TOKEN",
    "GROQ_TOKEN",
    "HF_TOKEN",
    "MIST_TOKEN",
    "SC_TOKEN",
    "SECRET",
    "BOT_POOL_URL",
    "proxies",
    "hist",
    "timeout",
    "get_gigachat_token",
    "send_prompt_async",
    "ask_DeepSeek_R1_async",
    "ask_DeepSeek_R1_Distill_Llama_70B_async",
    "ask_DeepSeek_V3_1_async",
    "ask_DeepSeek_V3_1_cb_async",
    "ask_DeepSeek_V3_2_async",
    "ask_Gemma_3_12b_it_async",
    "ask_Gpt_oss_120b_async",
    "ask_Llama_4_Maverick_17B_128E_Instruct_async",
    "ask_Meta_Llama_3_1_70B_Instruct_async",
    "ask_Meta_Llama_3_3_70B_Instruct_async",
    "ask_MiniMax_M2_5_async",
    "ask_MiniMax_M2_7_async",
    "ask_Mixtral_8x22b_async",
    "ask_Gemma_7b_async",
    "ask_Mistral_Nemo_Instruct_async",
    "ask_Web_DeepSeek_async",
    "ask_Web_DeepSeek_Thinking_async",
    "_post_to_bot_pool",
    "extract_choice_content",
]
