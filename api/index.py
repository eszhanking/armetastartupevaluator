import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

app = FastAPI(title="Armeta Vercel Hobby Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- РАБОТА С ХРАНИЛИЩЕМ ТОКЕНОВ (UPSTASH REDIS / VERCEL KV) ---
def get_redis_client():
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


def load_token() -> str:
    redis = get_redis_client()
    if redis:
        try:
            saved = redis.get("armeta_active_token")
            if saved:
                return str(saved)
        except Exception as e:
            print(f"[KV Error] Чтение токена: {e}")
    # Фолбэк на токен из переменных окружения
    return os.getenv("ARMETA_FALLBACK_TOKEN", "")


def save_token(new_token: str):
    redis = get_redis_client()
    if redis and new_token:
        try:
            redis.set("armeta_active_token", new_token)
            print("[KV] Свежий токен сохранен в хранилище.")
        except Exception as e:
            print(f"[KV Error] Запись токена: {e}")


def perform_login() -> str:
    """Логинится в Armeta по email/паролю и возвращает свежий JWT токен."""
    email = os.getenv("ARMETA_EMAIL")
    password = os.getenv("ARMETA_PASSWORD")

    if not email or not password:
        raise HTTPException(
            status_code=500,
            detail="В Environment Variables не заданы ARMETA_EMAIL и ARMETA_PASSWORD для авто-входа.",
        )

    login_url = "https://smeta.armeta.kz/api/auth/login"
    print(f"[Auth] Выполняем авто-логин для: {email}")

    resp = requests.post(
        login_url, json={"email": email, "password": password}, timeout=15
    )
    if resp.status_code in [200, 201]:
        data = resp.json()
        token = (
            data.get("access_token")
            or data.get("token")
            or data.get("jwt")
            or data.get("data", {}).get("token")
        )
        if token:
            save_token(token)
            return token
        raise HTTPException(
            status_code=500, detail=f"Сервер не вернул токен: {resp.text}"
        )
    raise HTTPException(
        status_code=resp.status_code,
        detail=f"Не удалось авторизоваться в Armeta: {resp.text}",
    )


def make_headers(token: str):
    formatted = token if token.startswith("Bearer ") else f"Bearer {token}"
    return {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "authorization": formatted,
        "content-type": "application/json",
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


class IdeaRequest(BaseModel):
    idea: str


# --- ОСНОВНОЙ ЭНДПОИНТ ---
@app.post("/api/evaluate")
def evaluate_idea(payload: IdeaRequest):
    # 1. Загружаем текущий токен
    token = load_token()
    if not token:
        # Если токена нет вовсе — сразу логинимся
        token = perform_login()

    headers = make_headers(token)

    masked_prompt = f"""Ты — продуктовый аналитик и технический архитектор IT-стартапов.
Проведи качественный продуктово-технический разбор идеи и гипотез проекта.

ПРОЕКТ:
{payload.idea}

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

    # Вспомогательная функция отправки сообщения с авто-повтором при 401
    def send_to_armeta(active_headers, retry_on_401=True):
        # Создаем чистый чат
        try:
            c_resp = requests.post(
                "https://smeta.armeta.kz/api/assistant/conversations",
                headers=active_headers,
                json={},
                timeout=15,
            )
            conv_id = (
                c_resp.json().get("id")
                if c_resp.status_code in [200, 201]
                else 1278
            )
        except Exception:
            conv_id = 1278

        msg_url = f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/messages"
        post_resp = requests.post(
            msg_url, headers=active_headers, json=request_data, timeout=30
        )

        # ЕСЛИ ТОКЕН ПРОТУХ (401) — ЛОГИНИМСЯ И ПОВТОРЯЕМ НА ЛЕТУ
        if post_resp.status_code == 401 and retry_on_401:
            print(
                "[401 Unauthorized] Токен устарел! Запускаем авто-обновление сессии..."
            )
            fresh_token = perform_login()
            new_headers = make_headers(fresh_token)
            return send_to_armeta(new_headers, retry_on_401=False)

        return post_resp, conv_id, active_headers

    post_response, conv_id, final_headers = send_to_armeta(headers)

    if post_response.status_code not in [200, 201]:
        raise HTTPException(
            status_code=post_response.status_code,
            detail=f"Ошибка Armeta ({post_response.status_code}): {post_response.text}",
        )

    target_assistant_id = post_response.json().get("assistant_message_id")
    stream_url = (
        f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/stream"
    )
    conversation_url = (
        f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}"
    )

    # Чтение SSE-потока (лимит Vercel теперь 300 секунд)
    collected_chunks = []
    try:
        with requests.get(
            stream_url, headers=final_headers, stream=True, timeout=120
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
    except Exception as e:
        print(f"[Stream]: {e}")

    result_text = "".join(collected_chunks).strip()
    if result_text:
        return {"success": True, "analysis": result_text}

    # Резервный опрос чата, если стрим отдал пустой буфер
    for _ in range(15):
        time.sleep(2)
        get_resp = requests.get(
            conversation_url, headers=final_headers, timeout=20
        )
        if get_resp.status_code == 200:
            data = get_resp.json()
            messages = (
                data.get("messages", []) if isinstance(data, dict) else data
            )
            for msg in reversed(messages):
                if (
                    msg.get("id") == target_assistant_id
                    or msg.get("role") == "assistant"
                ) and msg.get("content"):
                    return {"success": True, "analysis": msg.get("content")}

    return {
        "success": False,
        "analysis": "Сервис не успел сгенерировать ответ. Попробуйте еще раз.",
    }
