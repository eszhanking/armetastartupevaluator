import hashlib
import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import requests

app = FastAPI(title="Armeta Production Proxy with Auto-Auth")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def login_armeta() -> str | None:
    """Выполняет вход по логину и паролю, получает свежий JWT и сохраняет в Redis."""
    login = os.getenv("ARMETA_LOGIN")
    password = os.getenv("ARMETA_PASSWORD")

    if not login or not password:
        return None

    auth_urls = [
        "https://smeta.armeta.kz/api/auth/login",
        "https://smeta.armeta.kz/api/auth/signin",
        "https://smeta.armeta.kz/api/auth/token",
        "https://smeta.armeta.kz/api/v1/auth/login",
    ]

    base_headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://smeta.armeta.kz",
        "referer": "https://smeta.armeta.kz/login",
        "user-agent": "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36",
    }

    # Варианты полезной нагрузки (JSON)
    payloads = [
        {"email": login, "password": password},
        {"login": login, "password": password},
        {"username": login, "password": password},
    ]

    for url in auth_urls:
        for payload in payloads:
            try:
                resp = requests.post(
                    url, headers=base_headers, json=payload, timeout=12
                )
                if resp.status_code in [200, 201]:
                    data = resp.json()
                    new_token = (
                        data.get("token")
                        or data.get("access_token")
                        or data.get("jwt")
                        or (
                            data.get("data", {}).get("token")
                            if isinstance(data.get("data"), dict)
                            else None
                        )
                    )
                    if new_token:
                        cleaned = clean_jwt(new_token)
                        redis = get_redis()
                        if redis:
                            # Сохраняем на 11 часов (39600 сек)
                            try:
                                redis.set(
                                    "armeta_active_token", cleaned, ex=39600
                                )
                            except Exception:
                                pass
                        return cleaned
            except Exception:
                continue

    return None


def get_candidate_tokens() -> list[str]:
    candidates = []

    # 1. Сначала проверяем токен из Redis
    redis = get_redis()
    if redis:
        try:
            saved = redis.get("armeta_active_token")
            if saved:
                saved_clean = clean_jwt(str(saved))
                if saved_clean:
                    candidates.append(saved_clean)
        except Exception:
            pass

    # 2. Если в Redis нет актуального токена, пробуем автоматический логин
    if not candidates:
        fresh_token = login_armeta()
        if fresh_token:
            candidates.append(fresh_token)

    # 3. Резервный токен из переменных окружения
    env_token = clean_jwt(os.getenv("ARMETA_FALLBACK_TOKEN", ""))
    if env_token and env_token not in candidates:
        candidates.append(env_token)

    return candidates


def extract_piece_from_sse(raw_data: str) -> str:
    try:
        chunk = json.loads(raw_data)
        if isinstance(chunk, str):
            return chunk
        if isinstance(chunk, dict):
            for key in ("content", "delta", "text", "token", "message"):
                val = chunk.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, dict):
                    nested = val.get("content") or val.get("text")
                    if isinstance(nested, str) and nested:
                        return nested
        return ""
    except Exception:
        if not raw_data.startswith("{") and not raw_data.startswith("["):
            return raw_data
        return ""


@app.get("/")
def get_index():
    for path in ["public/index.html", "index.html", "../public/index.html"]:
        abs_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.exists(abs_p):
            return FileResponse(abs_p)
    return JSONResponse({"status": "API is running"})


@app.get("/api/status")
def check_status():
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
    return {
        "has_credentials": bool(
            os.getenv("ARMETA_LOGIN") and os.getenv("ARMETA_PASSWORD")
        ),
        "candidates_count": len(candidates),
        "checks": results,
    }


class IdeaRequest(BaseModel):
    idea: str


@app.post("/api/evaluate")
def evaluate_idea(payload: IdeaRequest):
    redis = get_redis()
    idea_clean = payload.idea.strip()

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
    if not candidates:
        raise HTTPException(
            status_code=500,
            detail="Не удалось получить рабочий токен даже через авто-логин. Проверьте ARMETA_LOGIN и ARMETA_PASSWORD.",
        )

    last_error = "Неизвестная ошибка при отправке"

    for token in candidates:
        headers = make_headers(token)
        conv_id = 1283
        msg_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/messages"

        post_resp = requests.post(
            msg_url, headers=headers, json=request_data, timeout=25
        )

        # Если 401 — пробуем экстренно перелогиниться на лету
        if post_resp.status_code == 401:
            fresh_token = login_armeta()
            if fresh_token:
                token = fresh_token
                headers = make_headers(token)
                post_resp = requests.post(
                    msg_url, headers=headers, json=request_data, timeout=25
                )

        if post_resp.status_code == 409:
            target_assistant_id = None
        elif post_resp.status_code in [200, 201]:
            post_json = post_resp.json()
            target_assistant_id = post_json.get("assistant_message_id")
            if (
                isinstance(post_json.get("content"), str)
                and post_json.get("content") != masked_prompt
            ):
                return {"success": True, "analysis": post_json["content"]}
        else:
            last_error = f"HTTP {post_resp.status_code}: {post_resp.text}"
            continue

        # Чтение ответа
        conversation_url = (
            f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}"
        )
        result_text = ""

        # Ускоренный опрос: до 35 секунд (17 попыток по 2 сек)
        for _ in range(17):
            time.sleep(2)
            try:
                g_resp = requests.get(
                    conversation_url, headers=headers, timeout=12
                )
                if g_resp.status_code == 200:
                    data = g_resp.json()
                    messages = (
                        data.get("messages", [])
                        if isinstance(data, dict)
                        else data
                    )
                    for msg in reversed(messages):
                        is_target = (
                            target_assistant_id
                            and msg.get("id") == target_assistant_id
                        )
                        is_assistant = msg.get("role") == "assistant"
                        content = msg.get("content")

                        if (
                            (is_target or is_assistant)
                            and isinstance(content, str)
                            and len(content.strip()) > 30
                        ):
                            result_text = content.strip()
                            break
            except Exception:
                continue

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
        detail=f"Ошибка авторизации Armeta: {last_error}",
    )