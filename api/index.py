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


def execute_auth_attempt(login: str, password: str):
    auth_urls = [
        "https://smeta.armeta.kz/api/auth/jwt/login",
        "https://smeta.armeta.kz/api/auth/login",
        "https://smeta.armeta.kz/api/auth/token",
        "https://smeta.armeta.kz/api/token",
        "https://smeta.armeta.kz/api/v1/auth/login",
    ]

    base_headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://smeta.armeta.kz",
        "referer": "https://smeta.armeta.kz/login",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    attempts_log = []

    for url in auth_urls:
        # 1. Формат Form-Urlencoded
        try:
            form_headers = dict(base_headers)
            form_headers["content-type"] = "application/x-www-form-urlencoded"
            resp = requests.post(
                url,
                headers=form_headers,
                data={"username": login, "password": password},
                timeout=8,
            )
            attempts_log.append(
                {
                    "url": url,
                    "type": "form",
                    "status": resp.status_code,
                    "text": resp.text[:120],
                }
            )
            if resp.status_code in [200, 201]:
                data = resp.json()
                tok = (
                    data.get("access_token")
                    or data.get("token")
                    or data.get("jwt")
                )
                if tok:
                    return clean_jwt(tok), attempts_log
        except Exception as e:
            attempts_log.append(
                {"url": url, "type": "form", "error": str(e)[:100]}
            )

        # 2. Формат JSON
        try:
            json_headers = dict(base_headers)
            json_headers["content-type"] = "application/json"
            for payload in [
                {"email": login, "password": password},
                {"username": login, "password": password},
            ]:
                resp = requests.post(
                    url, headers=json_headers, json=payload, timeout=8
                )
                attempts_log.append(
                    {
                        "url": url,
                        "type": "json",
                        "status": resp.status_code,
                        "text": resp.text[:120],
                    }
                )
                if resp.status_code in [200, 201]:
                    data = resp.json()
                    tok = (
                        data.get("token")
                        or data.get("access_token")
                        or data.get("jwt")
                        or (
                            data.get("data", {}).get("token")
                            if isinstance(data.get("data"), dict)
                            else None
                        )
                    )
                    if tok:
                        return clean_jwt(tok), attempts_log
        except Exception as e:
            attempts_log.append(
                {"url": url, "type": "json", "error": str(e)[:100]}
            )

    return None, attempts_log


def login_armeta() -> str | None:
    login = os.getenv("ARMETA_EMAIL") or os.getenv("ARMETA_LOGIN")
    password = os.getenv("ARMETA_PASSWORD")

    if not login or not password:
        return None

    tok, _ = execute_auth_attempt(login, password)
    if tok:
        redis = get_redis()
        if redis:
            try:
                redis.set("armeta_active_token", tok, ex=39600)
            except Exception:
                pass
    return tok


def get_candidate_tokens() -> list[str]:
    candidates = []

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

    if not candidates:
        fresh_token = login_armeta()
        if fresh_token:
            candidates.append(fresh_token)

    env_token = clean_jwt(os.getenv("ARMETA_FALLBACK_TOKEN", ""))
    if env_token and env_token not in candidates:
        candidates.append(env_token)

    return candidates


def create_fresh_conversation(headers: dict) -> int | None:
    """Создает изолированный новый чат, сбрасывая историю диалога."""
    for payload in [{}, {"title": "Аудит идеи"}]:
        try:
            resp = requests.post(
                "https://smeta.armeta.kz/api/assistant/conversations",
                headers=headers,
                json=payload,
                timeout=8,
            )
            if resp.status_code in [200, 201]:
                data = resp.json()
                cid = data.get("id") or data.get("conversation_id")
                if cid:
                    return int(cid)
        except Exception:
            continue
    return None


def extract_piece_from_sse(raw_data: str) -> str:
    """Извлекает токены из SSE-потока Armeta."""
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


@app.get("/api/debug-auth")
def debug_auth():
    login = os.getenv("ARMETA_EMAIL") or os.getenv("ARMETA_LOGIN")
    password = os.getenv("ARMETA_PASSWORD")

    if not login or not password:
        return {
            "error": "Переменные не найдены в процессе!",
            "ARMETA_EMAIL_found": bool(os.getenv("ARMETA_EMAIL")),
            "ARMETA_LOGIN_found": bool(os.getenv("ARMETA_LOGIN")),
            "ARMETA_PASSWORD_found": bool(os.getenv("ARMETA_PASSWORD")),
        }

    tok, logs = execute_auth_attempt(login, password)
    return {
        "success": bool(tok),
        "login_used": f"{login[:3]}***{login[-4:]}" if len(login) > 7 else "***",
        "token_received": f"{tok[:15]}...{tok[-10:]}" if tok else None,
        "attempts_log": logs,
    }


@app.get("/api/status")
def check_status():
    candidates = get_candidate_tokens()
    return {
        "has_credentials": bool(
            (os.getenv("ARMETA_EMAIL") or os.getenv("ARMETA_LOGIN"))
            and os.getenv("ARMETA_PASSWORD")
        ),
        "candidates_count": len(candidates),
        "token_preview": (
            f"{candidates[0][:15]}...{candidates[0][-10:]}"
            if candidates
            else None
        ),
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
            detail="Не удалось получить токен авторизации. Проверьте /api/debug-auth",
        )

    last_error_details = "Токены не смогли отправить запрос"

    for token in candidates:
        headers = make_headers(token)

        # 1. Создаем свежий изолированный диалог
        conv_id = create_fresh_conversation(headers)
        if not conv_id:
            fresh = login_armeta()
            if fresh:
                token = fresh
                headers = make_headers(token)
                conv_id = create_fresh_conversation(headers)

        if not conv_id:
            conv_id = 1283

        # 2. Отправляем запрос на создание сообщения
        msg_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/messages"
        try:
            post_resp = requests.post(
                msg_url, headers=headers, json=request_data, timeout=20
            )
        except Exception as e:
            last_error_details = f"Ошибка сети при отправке сообщения: {e}"
            continue

        if post_resp.status_code == 401:
            fresh_token = login_armeta()
            if fresh_token:
                token = fresh_token
                headers = make_headers(token)
                post_resp = requests.post(
                    msg_url, headers=headers, json=request_data, timeout=20
                )

        if post_resp.status_code not in [200, 201, 409]:
            last_error_details = (
                f"HTTP {post_resp.status_code}: {post_resp.text[:200]}"
            )
            continue

        target_assistant_id = None
        if post_resp.status_code in [200, 201]:
            post_json = post_resp.json()
            target_assistant_id = post_json.get("assistant_message_id")
            if (
                isinstance(post_json.get("content"), str)
                and post_json.get("content") != masked_prompt
            ):
                return {"success": True, "analysis": post_json["content"]}

        # 3. Подключаемся к SSE-стриму для запуска генерации на сервере
        stream_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/stream"
        collected_chunks = []
        try:
            with requests.get(
                stream_url, headers=headers, stream=True, timeout=30
            ) as stream_resp:
                if stream_resp.status_code == 200:
                    for line in stream_resp.iter_lines():
                        if not line:
                            continue
                        decoded = line.decode("utf-8", errors="ignore").strip()
                        if decoded.startswith("data:"):
                            raw = decoded[5:].strip()
                            if raw == "[DONE]":
                                break
                            part = extract_piece_from_sse(raw)
                            if part:
                                collected_chunks.append(part)
        except Exception:
            pass

        result_text = "".join(collected_chunks).strip()

        # 4. Если стрим отдал не весь текст, опрашиваем историю диалога
        if not result_text or len(result_text) < 40:
            conversation_url = (
                f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}"
            )
            for _ in range(12):
                time.sleep(1.5)
                try:
                    g_resp = requests.get(
                        conversation_url, headers=headers, timeout=8
                    )
                    if g_resp.status_code == 200:
                        messages = g_resp.json().get("messages", [])
                        for msg in reversed(messages):
                            content = msg.get("content", "")
                            if not isinstance(content, str):
                                continue

                            # Отсекаем только стартовое приветствие
                            if (
                                "здравствуйте! я ассистент по сметному ценообразованию"
                                in content.lower()
                            ):
                                continue

                            is_target = (
                                target_assistant_id
                                and msg.get("id") == target_assistant_id
                            )
                            is_assistant = msg.get("role") == "assistant"

                            if (
                                is_target or is_assistant
                            ) and len(content.strip()) > 40:
                                result_text = content.strip()
                                break
                except Exception:
                    continue

                if result_text:
                    break

        # Проверяем и возвращаем результат
        if result_text and len(result_text) > 30:
            if redis:
                try:
                    redis.set(cache_key, result_text, ex=259200)
                except Exception:
                    pass
            return {"success": True, "analysis": result_text}

        last_error_details = (
            "Стрим и опрос завершились, но нейросеть не вернула текст ответа"
        )

    raise HTTPException(
        status_code=504,
        detail=f"Ошибка обработки запроса: {last_error_details}",
    )