"""
FinSentry Resilient LLM Inference Engine
Orchestrates:
1. Local Ollama (`gemma4:e4b`) with optimized local settings (num_ctx: 4096, think: false, fast timeout)
2. Automatic seamless fallback to Gemini API (`GEMINI_API_KEY` from .env, using google-genai SDK)
3. Deterministic local regex/rule heuristics if neither is reachable
"""
import os
import re
import json
import logging
from typing import Optional, Dict, Any, Type
from pathlib import Path

import httpx
from pydantic import BaseModel
from dotenv import load_dotenv

# Load .env file
load_dotenv(override=True)

logger = logging.getLogger("finsentry.llm")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "8.0"))  # Fast failover to Gemini if Ollama is not running locally

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def clean_markdown_json(text: str) -> str:
    """Strips markdown code fences like ```json ... ```"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def call_local_ollama(prompt: str, image_b64: Optional[str] = None, json_schema: Optional[dict] = None) -> Optional[str]:
    """Calls local Ollama (gemma4:e4b) with optimized parameters."""
    payload: Dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "temperature": 0.1,
            "think": False,
            "num_predict": 1024,
        }
    }
    if image_b64:
        payload["images"] = [image_b64]
    if json_schema:
        payload["format"] = json_schema

    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            resp = client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
            if resp.status_code == 200:
                out = resp.json().get("response", "").strip()
                if out:
                    logger.info(f"Ollama ({OLLAMA_MODEL}) responded successfully.")
                    return out
    except Exception as e:
        logger.debug(f"Local Ollama connection bypassed/failed ({e}).")
    return None


def call_gemini_api(prompt: str, image_bytes: Optional[bytes] = None, mime_type: str = "image/jpeg", response_schema: Optional[Type[BaseModel]] = None) -> Optional[str]:
    """Calls Gemini API using google-genai SDK as primary cloud fallback."""
    api_key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
    if not api_key or api_key == "your_gemini_api_key_here":
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents = []

        if image_bytes:
            contents.append(
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            )
        contents.append(prompt)

        config_args = {
            "temperature": 0.1,
        }
        if response_schema:
            config_args["response_mime_type"] = "application/json"
            config_args["response_schema"] = response_schema

        config = types.GenerateContentConfig(**config_args)

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        if response and response.text:
            logger.info(f"Gemini API ({GEMINI_MODEL}) responded successfully.")
            return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini API fallback call failed: {e}")
    return None


def generate_structured_output(
    prompt: str,
    schema_cls: Type[BaseModel],
    image_b64: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg"
) -> Optional[BaseModel]:
    """
    Unified entrypoint:
    1. Try local Ollama (gemma4:e4b)
    2. Fallback to Gemini API
    3. Return validated Pydantic model
    """
    json_schema = schema_cls.model_json_schema()

    # Step 1: Local Ollama (gemma4:e4b)
    ollama_out = call_local_ollama(prompt, image_b64=image_b64, json_schema=json_schema)
    if ollama_out:
        try:
            cleaned = clean_markdown_json(ollama_out)
            return schema_cls.model_validate_json(cleaned)
        except Exception as e:
            logger.warning(f"Failed to validate Ollama output against {schema_cls.__name__}: {e}")

    # Step 2: Gemini API Fallback
    gemini_out = call_gemini_api(prompt, image_bytes=image_bytes, mime_type=mime_type, response_schema=schema_cls)
    if gemini_out:
        try:
            cleaned = clean_markdown_json(gemini_out)
            return schema_cls.model_validate_json(cleaned)
        except Exception as e:
            logger.warning(f"Failed to validate Gemini API output against {schema_cls.__name__}: {e}")

    return None
