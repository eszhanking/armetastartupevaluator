import hashlib
import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import requests

app = FastAPI(title="Armeta Production Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ваш свежий рабочий токен (tv: 1), вшитый напрямую на случай сбоя переменных Vercel
BUILTIN_FRESH_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI5NDkiLCJ0diI6MSwianRpIjoiREpHUnkyeG1IYk8zek1XZWxJbWhuQSIsImlhdCI6MTc5MjA5MjE1OSwiZXhwIjoxNzkyMTM1MzU5fQ._-e_SnM13ISUYqp-Ag2Vzw7e5ThPqtT_gW_5gxo-1xo"

COOKIES_STR = '_clck=1d78p6z%5E2%5Egab%5E0%5E2479; ui_variant=v1; g_state={"i_l":0,"i_ll":1792092157979,"i_e":{"enable_itp_optimization":24},"i_et":1792092157979,"i_b":"MQ8YyHG2REjZYj+IlsoimoaeqiqcZ6IJf3IfuzIbkkY"}; _clsk=1837rvr%5E1792092567099%5E16%5E1%5Ea.clarity.ms%2Fcollect'


def clean_jwt(raw_token: str) -> str:
    if not raw_token:
        return ""
    t = raw_token.strip().strip('"').strip("'")
    while t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def make_headers(raw_token: str):
    jwt_only = clean_jwt(raw_token)
    return {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": f"Bearer {jwt_only}",
        "content-type": "application/json",
        "cookie": COOKIES_STR,
        "origin": "https://smeta.armeta.kz",
        "priority": "u=1, i",
        "referer": "https://smeta.armeta.kz/chat",
        "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36",
    }


def get_redis():
    url = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("KV_REST_API_TOKEN") or os.getenv(
        "UPSTASH_REDIS_REST_TOKEN"
    )
    if url and token:
        try:
            from upstash_redis import Redis

            return Redis(url=url, token=token)
        except Exception:
            return None
    return None


def get_candidate_tokens() -> list[str]:
    """Собирает все доступные варианты токенов по приоритету."""
    candidates = []

    # 1. Токен из переменной окружения Vercel
    env_token = clean_jwt(os.getenv("ARMETA_FALLBACK_TOKEN", ""))
    if env_token:
        candidates.append(env_token)

    # 2. Вшитый свежий токен из вашего последнего cURL
    builtin = clean_jwt(BUILTIN_FRESH_TOKEN)
    if builtin and builtin not in candidates:
        candidates.append(builtin)

    # 3. Токен из Redis (если есть)
    redis = get_redis()
    if redis:
        try:
            saved = redis.get("armeta_active_token")
            if saved:
                saved_clean = clean_jwt(str(saved))
                if saved_clean and saved_clean not in candidates:
                    candidates.insert(0, saved_clean)
        except Exception:
            pass

    return candidates


@app.get("/")
def get_index():
    for path in ["public/index.html", "index.html", "../public/index.html"]:
        abs_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.exists(abs_p):
            return FileResponse(abs_p)
    return JSONResponse({"status": "API is running"})


@app.get("/api/status")
def check_status():
    """Эндпоинт для проверки связи с Armeta прямо из браузера."""
    candidates = get_candidate_tokens()
    results = []
    for idx, tok in enumerate(candidates):
        headers = make_headers(tok)
        try:
            r = requests.get(
                "https://smeta.armeta.kz/api/assistant/conversations/1283",
                headers=headers,
                timeout=10,
            )
            results.append(
                {
                    "token_index": idx,
                    "token_preview": f"{tok[:15]}...{tok[-10:]}",
                    "armeta_status_code": r.status_code,
                    "is_working": r.status_code == 200,
                }
            )
        except Exception as e:
            results.append({"token_index": idx, "error": str(e)})
    return {"candidates_count": len(candidates), "checks": results}


class IdeaRequest(BaseModel):
    idea: str


@app.post("/api/evaluate")
def evaluate_idea(payload: IdeaRequest):
    redis = get_redis()
    idea_clean = payload.idea.strip()

    # Проверка кэша
    idea_hash = hashlib.md5(idea_clean.lower().encode("utf-8")).hexdigest()
    cache_key = f"cache:eval:{idea_hash}"
    if redis:
        try:
            cached = redis.get(cache_key)
            if cached:
                return {"success": True, "analysis": str(cached)}
        except Exception:
            pass

    masked_prompt = f"""Ты — продуктовый аналитик и технический архитектор IT-стартапов.
Проведи качественный продуктово-технический разбор идеи и гипотез проекта.

ПРОЕКТ:
{idea_clean}

ФОРМАТ И ПРАВИЛА:
- Не придумывай точные финансовые цифры или ставки специалистов, если у тебя нет актуальных рыночных прайсов. Вместо этого разбери структуру, логику и порядок величин.
- Пиши по делу, конкретно, без вводных приветствий и без нравоучений.
- Оформи ответ строго в формате Markdown по структуре ниже.

# Продуктово-технический аудит идеи

### 1. Реалистичность гипотезы и ценность для пользователя
(В чем главная иллюзия основателя, где проект столкнется с ленью пользователей или нехваткой данных)

### 2. Технические барьеры и архитектурные грабли
(Узкие места в технологиях, интеграциях, API или обработке данных)

### 3. Структура затрат и модель монетизации
(Какие статьи расходов станут основными пожирателями бюджета, на чем может сломаться юнит-экономика)

### 4. План проверки гипотезы (MVP с минимальным бюджетом)
(3 конкретных шага, как проверить жизнеспособность идеи за 1-2 недели без сложной разработки)
"""

    request_data = {
        "content": masked_prompt,
        "as_of": None,
        "estimate_uid": None,
        "attachment_ids": [],
    }

    candidates = get_candidate_tokens()
    last_error = "Не найдено ни одного токена"

    # Пробуем каждый доступный токен, пока один не сработает
    for token in candidates:
        headers = make_headers(token)

        # Создаем новый диалог (или используем 1283 из вашего последнего cURL)
        conv_id = 1283
        try:
            c_resp = requests.post(
                "https://smeta.armeta.kz/api/assistant/conversations",
                headers=headers,
                json={},
                timeout=15,
            )
            if c_resp.status_code in [200, 201]:
                conv_id = c_resp.json().get("id", 1283)
        except Exception:
            pass

        msg_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/messages"
        post_resp = requests.post(
            msg_url, headers=headers, json=request_data, timeout=30
        )

        # Если этот токен не подошел — переходим к следующему
        if post_resp.status_code in [401, 403] or "authentication" in post_resp.text.lower():
            last_error = f"Код {post_resp.status_code}: {post_resp.text}"
            continue

        if post_resp.status_code not in [200, 201]:
            last_error = f"Код {post_resp.status_code}: {post_resp.text}"
            continue

        # Токен сработал! Считываем поток
        target_assistant_id = post_resp.json().get("assistant_message_id")
        stream_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/stream"
        conversation_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}"

        collected_chunks = []
        try:
            with requests.get(
                stream_url, headers=headers, stream=True, timeout=120
            ) as stream_resp:
                if stream_resp.status_code == 200:
                    for line in stream_resp.iter_lines():
                        if not line:
                            continue
                        decoded = line.decode("utf-8", errors="ignore").strip()
                        if decoded.startswith("data:"):
                            raw_data = decoded[5:].strip()
                            if raw_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(raw_data)
                                piece = (
                                    chunk.get("content")
                                    or chunk.get("delta")
                                    or chunk.get("text")
                                )
                                if piece:
                                    collected_chunks.append(piece)
                            except Exception:
                                if raw_data:
                                    collected_chunks.append(raw_data)
        except Exception:
            pass

        result_text = "".join(collected_chunks).strip()

        # Если стрим не отдал текст напрямую, забираем из истории чата
        if not result_text:
            for _ in range(15):
                time.sleep(2)
                g_resp = requests.get(
                    conversation_url, headers=headers, timeout=20
                )
                if g_resp.status_code == 200:
                    data = g_resp.json()
                    messages = (
                        data.get("messages", [])
                        if isinstance(data, dict)
                        else data
                    )
                    for msg in reversed(messages):
                        if (
                            msg.get("id") == target_assistant_id
                            or msg.get("role") == "assistant"
                        ) and msg.get("content"):
                            result_text = msg.get("content")
                            break
                if result_text:
                    break

        if result_text:
            if redis:
                try:
                    redis.set(cache_key, result_text, ex=259200)
                except Exception:
                    pass
            return {"success": True, "analysis": result_text}

    raise HTTPException(
        status_code=401,
        detail=f"Все токены отклонены Armeta. Последний ответ: {last_error}",
    )