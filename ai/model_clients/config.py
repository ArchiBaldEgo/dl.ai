"""Centralized configuration for external AI model APIs."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

CLIENT_ID = os.getenv("CLIENT_ID")
SECRET = os.getenv("SBER_SECRET")
HF_TOKEN = os.getenv("HF_TOKEN")
SC_TOKEN = os.getenv("SC_TOKEN")
MIST_TOKEN = os.getenv("MIST_TOKEN")
GROQ_TOKEN = os.getenv("GROQ_TOKEN")
DEEPSEEK_API_TOKEN = os.getenv("DEEPSEEK_API_TOKEN") or os.getenv("DEEPSEEK_API_KEY")

BOT_POOL_URL = os.getenv("BOT_POOL_URL", "http://localhost:3000").rstrip("/")

PROXY = os.getenv("PROXY")
proxies = {"http": PROXY, "https": PROXY} if PROXY else None

# Centralized SambaCloud model IDs to avoid code edits on every deprecation.
SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B = os.getenv(
    "SAMBANOVA_MODEL_DEEPSEEK_R1_DISTILL_LLAMA_70B",
    "DeepSeek-R1-Distill-Llama-70B",
)
SAMBANOVA_MODEL_DEEPSEEK_V3_1 = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_1", "DeepSeek-V3.1")
SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_1_CB", "DeepSeek-V3.1-cb")
SAMBANOVA_MODEL_DEEPSEEK_V3_2 = os.getenv("SAMBANOVA_MODEL_DEEPSEEK_V3_2", "DeepSeek-V3.2")
SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT = os.getenv(
    "SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT",
    "Llama-4-Maverick-17B-128E-Instruct",
)
SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT = os.getenv(
    "SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT",
    "Meta-Llama-3.3-70B-Instruct",
)
SAMBANOVA_MODEL_MINIMAX_M2_5 = os.getenv("SAMBANOVA_MODEL_MINIMAX_M2_5", "MiniMax-M2.5")
SAMBANOVA_MODEL_MINIMAX_M2_7 = os.getenv("SAMBANOVA_MODEL_MINIMAX_M2_7", "MiniMax-M2.7")
SAMBANOVA_MODEL_GEMMA_3_12B_IT = os.getenv("SAMBANOVA_MODEL_GEMMA_3_12B_IT", "gemma-3-12b-it")
SAMBANOVA_MODEL_GPT_OSS = os.getenv("SAMBANOVA_MODEL_GPT_OSS", "gpt-oss-120b")

# Backward-compatible env names used in older code paths.
SAMBANOVA_MODEL_DEEPSEEK = os.getenv("SAMBANOVA_MODEL_DEEPSEEK", SAMBANOVA_MODEL_DEEPSEEK_V3_1)
SAMBANOVA_MODEL_META = os.getenv("SAMBANOVA_MODEL_META", SAMBANOVA_MODEL_META_LLAMA_3_3_70B_INSTRUCT)
SAMBANOVA_MODEL_MIXTRAL_ALIAS = os.getenv(
    "SAMBANOVA_MODEL_MIXTRAL_ALIAS",
    SAMBANOVA_MODEL_LLAMA_4_MAVERICK_17B_128E_INSTRUCT,
)
