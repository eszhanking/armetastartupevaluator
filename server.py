import os
import time
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Armeta Bridge Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Базовые заголовки из вашего cURL
HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI5NDkiLCJ0diI6MCwianRpIjoiWVJkWmEtY3ljcE5iU1V3VlBZekh4USIsImlhdCI6MTc5MjA3NzUwOSwiZXhwIjoxNzkyMTIwNzA5fQ.REm4CThwqvfCko6ZeagVSlk1cDYJUDCmh3NBciTTRQE",
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
    "cookie": '_clck=1d78p6z%5E2%5Egab%5E0%5E2479; g_state={"i_l":0,"i_ll":1792077500479,"i_e":{"enable_itp_optimization":24},"i_et":1792077500479}; ui_variant=v1; _clsk=2nbuh5%5E1792085836879%5E7%5E1%5Ea.clarity.ms%2Fcollect',
}


@app.get("/")
def get_index():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return JSONResponse(
        status_code=404,
        content={"error": "Положите файл index.html в одну папку с server.py"},
    )


class IdeaRequest(BaseModel):
    idea: str


def get_fresh_conversation_id():
    """Создает новый чистый чат, чтобы не конфликтовать со старыми запросами."""
    try:
        url = "https://smeta.armeta.kz/api/assistant/conversations"
        r = requests.post(url, headers=HEADERS, json={}, timeout=10)
        if r.status_code in [200, 201]:
            new_id = r.json().get("id")
            print(f"[+] Создан новый чистый диалог ID: {new_id}")
            return new_id
    except Exception as e:
        print(f"[!] Не удалось создать новый чат, используем резервный: {e}")
    return 1278


@app.post("/api/evaluate")
def evaluate_idea(payload: IdeaRequest):
    print(f"\n[1/3] Идея получена: {payload.idea[:50]}...")

    # Создаем свежий чат
    conv_id = get_fresh_conversation_id()
    messages_url = (
        f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}/messages"
    )
    conversation_url = (
        f"https://smeta.armeta.kz/api/assistant/conversations/{conv_id}"
    )

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

    try:
        print(f"[2/3] Отправка запроса в чат {conv_id}...")
        post_response = requests.post(
            messages_url, headers=HEADERS, json=request_data, timeout=30
        )

        target_assistant_id = None

        if post_response.status_code == 409:
            print(
                "[!] Получен статус 409 (ответ еще генерируется). Переходим к ожиданию готового текста..."
            )
            time.sleep(3)
        elif post_response.status_code in [200, 201]:
            post_data = post_response.json()
            target_assistant_id = post_data.get("assistant_message_id")
            print(f"[2/3] Сообщение принято! ID ответа: {target_assistant_id}")
        else:
            raise HTTPException(
                status_code=post_response.status_code,
                detail=f"Ошибка Armeta: {post_response.text}",
            )

        # Ожидание генерации ответа
        print("[3/3] Ожидание генерации ответа от нейросети...")
        for attempt in range(1, 40):
            time.sleep(1.5)

            get_response = requests.get(
                conversation_url, headers=HEADERS, timeout=30
            )
            if get_response.status_code != 200:
                continue

            try:
                conv_data = get_response.json()
            except Exception:
                continue

            messages = []
            if isinstance(conv_data, dict):
                messages = conv_data.get("messages", [])
            elif isinstance(conv_data, list):
                messages = conv_data

            for msg in reversed(messages):
                is_target = (
                    target_assistant_id and msg.get("id") == target_assistant_id
                )
                is_assistant = msg.get("role") == "assistant"

                content = msg.get("content")
                if (is_target or is_assistant) and content:
                    print(f"[3/3] Готово! Ответ успешно получен.")
                    return {"success": True, "analysis": content}

        return {
            "success": True,
            "analysis": "Ответ генерируется дольше обычного. Попробуйте обновить страницу.",
        }

    except requests.exceptions.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Сетевая ошибка с Armeta: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)