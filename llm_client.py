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

LMSTUDIO_HOST = os.getenv("LMSTUDIO_HOST", "http://127.0.0.1:1234/v1").rstrip("/")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "google/gemma-4-e4b")
LMSTUDIO_TIMEOUT = float(os.getenv("LMSTUDIO_TIMEOUT", "45.0"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "30.0"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "4.0"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
]


def clean_markdown_json(text: str) -> str:
    """Strips markdown code fences like ```json ... ```"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def call_groq_vision(prompt: str, image_b64: str, mime_type: str = "image/jpeg", json_schema: Optional[dict] = None) -> Optional[str]:
    """Calls Groq OpenAI-compatible Vision API (qwen/qwen3.8-27b) for bill extraction."""
    load_dotenv(override=True)
    api_key = os.getenv("GROQ_API_KEY", GROQ_API_KEY).strip()
    if not api_key or api_key == "your_groq_api_key_here":
        logger.error("GROQ_API_KEY is missing or not configured in .env.")
        return None

    content = [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
        }
    ]

    payload: Dict[str, Any] = {
        "model": os.getenv("GROQ_VISION_MODEL", GROQ_VISION_MODEL),
        "messages": [
            {
                "role": "system",
                "content": "You are a precise corporate financial audit AI. Return strictly valid raw JSON matching the requested schema without markdown formatting or conversational filler."
            },
            {"role": "user", "content": content}
        ],
        "temperature": 0.1,
    }

    if json_schema:
        payload["response_format"] = {"type": "json_object"}
        payload["messages"][0]["content"] += f"\nJSON Schema:\n{json.dumps(json_schema)}"

    try:
        with httpx.Client(timeout=GROQ_TIMEOUT) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    out = choices[0].get("message", {}).get("content", "").strip()
                    if out:
                        logger.info(f"Groq Vision ({payload['model']}) responded successfully.")
                        return out
            else:
                logger.warning(f"Groq Vision non-200 status: {resp.status_code} - {resp.text[:160]}")
    except Exception as e:
        logger.warning(f"Groq Vision call failed: {e}")
    return None


def call_local_lmstudio(prompt: str, image_b64: Optional[str] = None, mime_type: str = "image/jpeg", json_schema: Optional[dict] = None) -> Optional[str]:
    """Calls local LM Studio OpenAI-compatible endpoint with Gemma 4 e4b."""
    content: Any
    if image_b64:
        content = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{image_b64}"
                }
            }
        ]
    else:
        content = prompt

    payload: Dict[str, Any] = {
        "model": LMSTUDIO_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise corporate financial audit AI. When requested, return strictly valid raw JSON without markdown formatting or conversational filler."
            },
            {
                "role": "user",
                "content": content
            }
        ],
        "temperature": 0.1,
    }

    if json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "AuditResponse",
                "strict": True,
                "schema": json_schema
            }
        }

    try:
        with httpx.Client(timeout=LMSTUDIO_TIMEOUT) as client:
            resp = client.post(f"{LMSTUDIO_HOST}/chat/completions", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    out = choices[0].get("message", {}).get("content", "").strip()
                    if out:
                        logger.info(f"Local LM Studio ({LMSTUDIO_MODEL}) responded successfully.")
                        return out
            else:
                logger.debug(f"LM Studio non-200 status: {resp.status_code} - {resp.text[:120]}")
    except Exception as e:
        logger.debug(f"Local LM Studio connection bypassed/failed ({e}).")
    return None


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

        target_models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
        for m in target_models:
            try:
                response = client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config,
                )
                if response and response.text:
                    logger.info(f"Gemini API ({m}) responded successfully.")
                    return response.text.strip()
            except Exception as model_err:
                logger.warning(f"Gemini model {m} attempt error: {model_err}")
                continue
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
    1. Try local LM Studio (google/gemma-4-e4b) - zero rate limit, offline local MLX
    2. Try local Ollama (gemma4:e4b)
    3. Fallback to Gemini API
    4. Return validated Pydantic model
    """
    json_schema = schema_cls.model_json_schema()

    # If image_bytes provided but not image_b64, encode it
    if image_bytes and not image_b64:
        import base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    # For Vision / Image OCR: Groq Vision (qwen/qwen3.8-27b) ONLY — no fallbacks.
    if image_bytes or image_b64:
        if image_b64:
            groq_out = call_groq_vision(prompt, image_b64=image_b64, mime_type=mime_type, json_schema=json_schema)
            if groq_out:
                try:
                    cleaned = clean_markdown_json(groq_out)
                    return schema_cls.model_validate_json(cleaned)
                except Exception as e:
                    logger.warning(f"Failed to validate Groq Vision output against {schema_cls.__name__}: {e}")
        return None

    # Step 1 (Text): Local LM Studio (Gemma 4 e4b MLX 4bit)
    lmstudio_out = call_local_lmstudio(prompt, image_b64=image_b64, mime_type=mime_type, json_schema=json_schema)
    if lmstudio_out:
        try:
            cleaned = clean_markdown_json(lmstudio_out)
            return schema_cls.model_validate_json(cleaned)
        except Exception as e:
            logger.warning(f"Failed to validate LM Studio output against {schema_cls.__name__}: {e}")

    # Step 2: Local Ollama (gemma4:e4b)
    ollama_out = call_local_ollama(prompt, image_b64=image_b64, json_schema=json_schema)
    if ollama_out:
        try:
            cleaned = clean_markdown_json(ollama_out)
            return schema_cls.model_validate_json(cleaned)
        except Exception as e:
            logger.warning(f"Failed to validate Ollama output against {schema_cls.__name__}: {e}")

    # Step 3: Gemini API Fallback
    gemini_out = call_gemini_api(prompt, image_bytes=image_bytes, mime_type=mime_type, response_schema=schema_cls)
    if gemini_out:
        try:
            cleaned = clean_markdown_json(gemini_out)
            return schema_cls.model_validate_json(cleaned)
        except Exception as e:
            logger.warning(f"Failed to validate Gemini API output against {schema_cls.__name__}: {e}")

    return None
