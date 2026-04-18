#!/usr/bin/env python3

"""Reusable Qwen chat completion client."""

from typing import Dict, List


models_by_tier: Dict[str, List[str]] = {
    "large": [
        # --- Ultra-Large & Specialized Frontier (480B - 235B) ---
        "qwen3-coder-480b-a35b-instr",
        "qwen3.5-397b-a17b",
        "qwen3-235b-a22b-thinking-2",
        "qwen3-235b-a22b-instruct-2",
        "qwen3-vl-235b-a22b-instruct",
        "qwen3-235b-a22b",

        # --- "Max" Tier (Flagship API Endpoints, ~100B+ capacity) ---
        "qvq-max-latest",
        "qwen-vl-max-latest",
        "qwen-max-latest",
        "qwen3-max-preview",
        "qwen3-max-2026-01-23",
        "qwen-vl-max-2025-08-13",
        "qwen-max-2025-01-25",
        "qwen3-max",
        "qwen-max"
    ],
    "medium": [
        # --- High-Capacity Open Weights (80B - 72B) ---
        "qwen3-next-80b-a3b-thinking",
        "qwen3-next-80b-a3b-instruct",
        "qwen2.5-72b-instruct",

        # --- "Plus" Tier (High-Tier API Endpoints, ~30B-70B capacity) ---
        "qwq-plus",
        "qwen-plus-latest",
        "qwen-vl-plus-latest",
        "qwen3.6-plus-2026-04-02",
        "qwen3.5-plus-2026-02-15",
        "qwen3-vl-plus-2025-12-19",
        "qwen3-coder-plus-2025-09-23",
        "qwen3-vl-plus-2025-09-23",
        "qwen-plus-2025-09-11",
        "qwen-plus-2025-07-28",
        "qwen3-coder-plus-2025-07-22",
        "qwen-plus-2025-07-14",
        "qwen-vl-plus-2025-05-07",
        "qwen-plus-2025-04-28",
        "qwen-vl-plus-2025-01-25",
        "qwen-plus-2025-01-25",
        "qwen-mt-plus",
        "qwen-plus-character-ja",
        "qwen-vl-plus",
        "qwen-plus",

        # --- Mid-High Open Weights (35B - 27B) ---
        "qwen3.5-35b-a3b",
        "qwen2.5-vl-32b-instruct",
        "qwen2.5-32b-instruct",
        "qwen3-30b-a3b-thinking-2507",
        "qwen3-30b-a3b-instruct-2507",
        "qwen3-coder-30b-a3b-instruct",
        "qwen3-30b-a3b",
        "qwen3.5-27b"
    ],
    "small": [
        # --- "Turbo" Tier (Mid-Fast API Endpoints, ~14B capacity) ---
        "qwen-turbo-latest",
        "qwen-turbo-2024-11-01",
        "qwen-turbo",

        # --- Mid-Size Open Weights (14B) ---
        "qwen3-14b",
        "qwen2.5-14b-instruct-1m",
        "qwen2.5-14b-instruct",

        # --- "Flash" Tier (Ultra-Fast API Endpoints, ~7B-8B capacity) ---
        "qwen3.6-flash",
        "qwen3-vl-flash-2025-10-15",
        "qwen-flash-2025-07-28",
        "qwen3-coder-flash-2025-07-28",
        "wan2.2-kf2v-flash",
        "qwen-mt-flash",
        "qwen3-vl-flash",
        "qwen-flash",

        # --- Base Open Weights (8B - 7B) ---
        "qwen3-vl-8b-instruct",
        "qwen3-8b",
        "qwen2.5-7b-instruct-1m",
        "qwen2.5-vl-7b-instruct",
        "qwen2.5-7b-instruct",
    ]
}

API_KEY = "sk-81598295d5044d6e84c568ab9fc18874"
BASE_URL = "https://dashscope-us.aliyuncs.com/compatible-mode/v1"