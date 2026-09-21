import json
import threading
import time
import requests

from config import (
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_TIMEOUT,
    FAST_PATH_ENABLED,
    PROFILING_ENABLED,
    WARMUP_ENABLED,
)

SYSTEM_PROMPT = """Ты — интеллектуальный модуль распознавания команд для голосового ассистента Аврора.
Твоя задача — проанализировать текст, сказанный пользователем, и вернуть JSON с действием, которое нужно выполнить.

Доступные программы на ПК (для действия open_program):
- блокнот
- калькулятор
- проводник
- диспетчер задач
- командная строка
- paint (или пеинт)
- браузер (хром, опера, яндекс и т.д.)
- стим (steam)
- дискорд (discord)
- телеграм (telegram)
* Примечание: ИИ может также распознать любую другую программу, которую просит пользователь, вернув её оригинальное название.

Формат ответа должен быть строго в JSON:
{
  "action": "open_program" | "search_web" | "hide" | "exit" | "unknown",
  "program_key": "название_программы_на_русском_или_английском_или_пустая_строка",
  "query": "запрос_для_поиска_в_интернете_или_пустая_строка"
}

Примеры:
1. "открой блокнот" -> {"action": "open_program", "program_key": "блокнот", "query": ""}
2. "мне нужно сделать запись" -> {"action": "open_program", "program_key": "блокнот", "query": ""}
3. "посчитай сколько будет 5 плюс 5" -> {"action": "open_program", "program_key": "калькулятор", "query": ""}
4. "запусти стим пожалуйста" -> {"action": "open_program", "program_key": "steam", "query": ""}
5. "найди рецепт блинов" -> {"action": "search_web", "program_key": "", "query": "рецепт блинов"}
6. "кто такой Юрий Гагарин" -> {"action": "search_web", "program_key": "", "query": "кто такой Юрий Гагарин"}
7. "выключись" -> {"action": "exit", "program_key": "", "query": ""}
8. "закройся" -> {"action": "hide", "program_key": "", "query": ""}
9. "привет, как твои дела" -> {"action": "unknown", "program_key": "", "query": ""}
"""

# Постоянная HTTP-сессия для keep-alive соединений к Ollama
_session = requests.Session()

# Маркеры для Fast Path Router
_EXIT_PHRASES = frozenset([
    "выключись", "выключи себя", "стоп аврора", "стоп", "выход",
    "отключись", "завершить работу", "заверши работу"
])

_HIDE_PHRASES = frozenset([
    "свернись", "скройся", "скрой окно", "свернуть",
    "закройся", "закрой окно", "свернись в трей"
])

_SEARCH_PREFIXES = (
    "найди в интернете", "поиск в интернете", "найди в гугле",
    "найди в яндексе", "найди в браузере", "поищи в интернете",
    "поищи в гугле", "поищи"
)

_OPEN_PREFIXES = (
    "открой программу", "запусти программу", "включи программу",
    "открой", "запусти", "включи"
)

_AMBIGUOUS_WORDS = frozenset([
    "но", "не", "что-нибудь", "какую-нибудь", "какую-то",
    "забыл", "если", "когда", "хочу", "можешь", "умеешь"
])


def fast_route_command(text):
    """
    Консервативный локальный Command Router.
    Возвращает dict с действием, если команда однозначна (Fast Path),
    либо None, если команда сложная, разговорная или неоднозначная (передача в LLM).
    """
    clean_text = text.lower().strip(" ,.?!«»")
    if not clean_text:
        return None

    # 1. Завершение работы
    if clean_text in _EXIT_PHRASES:
        return {"action": "exit", "program_key": "", "query": "", "source": "fast_path"}

    # 2. Сворачивание в трей
    if clean_text in _HIDE_PHRASES:
        return {"action": "hide", "program_key": "", "query": "", "source": "fast_path"}

    # 3. Веб-поиск по явным маркерам
    for prefix in _SEARCH_PREFIXES:
        if clean_text.startswith(prefix):
            query = clean_text[len(prefix):].strip(" ,.?!")
            if query and len(query) >= 2:
                return {"action": "search_web", "program_key": "", "query": query, "source": "fast_path"}

    if clean_text.startswith("найди "):
        query = clean_text[6:].strip(" ,.?!")
        # Не перехватываем "найди программу..." или "найди файл..."
        if query and not query.startswith(("программу", "файл", "где")):
            return {"action": "search_web", "program_key": "", "query": query, "source": "fast_path"}

    # 4. Запуск программ по явным глаголам действия
    for prefix in _OPEN_PREFIXES:
        if clean_text.startswith(prefix + " "):
            arg = clean_text[len(prefix) + 1:].strip(" ,.?!")
            words = arg.split()

            # Консервативная проверка:
            # - длина аргумента от 1 до 4 слов
            # - нет слов неуверенности/сомнений ("но не знаю какую", "что-нибудь")
            if 1 <= len(words) <= 4 and not any(w in words for w in _AMBIGUOUS_WORDS):
                # Удаляем вежливые слова ("пожалуйста", "плиз")
                clean_name = " ".join([w for w in words if w not in ("пожалуйста", "плиз")]).strip()
                if clean_name:
                    return {"action": "open_program", "program_key": clean_name, "query": "", "source": "fast_path"}

    # Не уверены на 100% -> отдаем сложную фразу в LLM
    return None


def fallback_parser(text):
    """Резервный локальный парсер на случай сбоя Ollama."""
    route = fast_route_command(text)
    if route:
        return route

    # Если даже Fast Path не помог, базовый fallback в поиск
    return {"action": "search_web", "program_key": "", "query": text, "source": "fallback"}


def warmup_ollama():
    """Фоновый прогрев модели в Ollama при старте Авроры."""
    if not WARMUP_ENABLED:
        return

    def _do_warmup():
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": "ping",
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        }
        try:
            t0 = time.perf_counter()
            _session.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            if PROFILING_ENABLED:
                print(f"[Мозг] Ollama ({OLLAMA_MODEL}) успешно прогрета в RAM за {time.perf_counter() - t0:.2f}с")
        except Exception as e:
            if PROFILING_ENABLED:
                print(f"[Мозг] Фоновый прогрев Ollama пропущен (сервер не ответил): {e}")

    threading.Thread(target=_do_warmup, daemon=True, name="OllamaWarmup").start()


def parse_command_with_ai(text):
    """
    Основная точка входа разбора команд.
    Сначала проверяет Fast Path Router (< 1 мс).
    Если команда неоднозначна — отправляет запрос в Ollama через persistent session.
    """
    t_start = time.perf_counter()

    # 1. Проверяем Fast Path Router
    if FAST_PATH_ENABLED:
        fast_result = fast_route_command(text)
        if fast_result is not None:
            if PROFILING_ENABLED:
                elapsed = (time.perf_counter() - t_start) * 1000
                print(f"[РОУТЕР: FastPath {elapsed:.3f} мс] Действие: {fast_result.get('action')}")
            return fast_result

    # 2. Неоднозначная команда -> отправляем в Ollama (LLM)
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{SYSTEM_PROMPT}\n\nПользователь сказал: \"{text}\"\nВерни только JSON:",
        "stream": False,
        "format": "json",
        "keep_alive": OLLAMA_KEEP_ALIVE,  # Предотвращает выгрузку модели из RAM
    }

    try:
        response = _session.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        response.raise_for_status()

        result_json = response.json()
        response_text = result_json.get("response", "").strip()

        data = json.loads(response_text)
        if PROFILING_ENABLED:
            elapsed = time.perf_counter() - t_start
            print(f"[МОЗГ: Ollama {elapsed:.2f} с] Действие: {data.get('action')}")
        return data

    except requests.exceptions.RequestException as e:
        print(f"[Мозг] Ошибка связи с Ollama. Использую локальный fallback: {e}")
        return fallback_parser(text)
    except Exception as e:
        print(f"[Мозг] Ошибка разбора ответа ИИ: {e}. Использую локальный fallback.")
        return fallback_parser(text)
