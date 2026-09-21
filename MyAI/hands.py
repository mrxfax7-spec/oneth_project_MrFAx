import os
import subprocess
import webbrowser
import threading
import time
from config import PROFILING_ENABLED, PROGRAM_CACHE_ENABLED
from brain import parse_command_with_ai

# Словарь стандартных встроенных приложений Windows (запуск без поиска по диску)
BUILTIN_APPS = {
    "блокнот": "notepad.exe",
    "notepad": "notepad.exe",
    "калькулятор": "calc.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "проводник": "explorer.exe",
    "explorer": "explorer.exe",
    "диспетчер задач": "taskmgr.exe",
    "taskmgr": "taskmgr.exe",
    "командная строка": "cmd.exe",
    "cmd": "cmd.exe",
    "paint": "mspaint.exe",
    "пеинт": "mspaint.exe",
    "паинт": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "панель управления": "control.exe",
    "реестр": "regedit.exe",
    "терминал": "wt.exe",
}

# Кэш ярлыков в оперативной памяти и блокировка для потокобезопасности
_shortcuts_cache = []  # список кортежей (shortcut_name_lower, full_path, original_name)
_shortcuts_indexed = False
_cache_lock = threading.Lock()


def index_shortcuts(force=False):
    """Однократно сканирует меню 'Пуск' и сохраняет ярлыки в памяти."""
    global _shortcuts_cache, _shortcuts_indexed
    with _cache_lock:
        if _shortcuts_indexed and not force:
            return _shortcuts_cache

        start_menu_paths = [
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
            os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")
        ]

        new_cache = []
        for path in start_menu_paths:
            if os.path.exists(path):
                for root, _, files in os.walk(path):
                    for file in files:
                        if file.lower().endswith(".lnk"):
                            shortcut_name = file[:-4].lower()
                            full_path = os.path.join(root, file)
                            new_cache.append((shortcut_name, full_path, file[:-4]))

        _shortcuts_cache = new_cache
        _shortcuts_indexed = True
        return _shortcuts_cache


def find_and_run_program(name):
    """Быстрый поиск программы в кэше ярлыков меню 'Пуск' Windows и её запуск."""
    name = name.lower().strip()
    if not name:
        print("[Руки] Имя программы не указано.")
        return False

    t_start = time.perf_counter()

    # 1. Быстрая проверка встроенных утилит Windows (мгновенный запуск)
    if name in BUILTIN_APPS:
        exe = BUILTIN_APPS[name]
        try:
            os.startfile(exe)
            if PROFILING_ENABLED:
                print(f"[Руки] Запуск встроенной утилиты '{exe}' за {(time.perf_counter() - t_start)*1000:.2f} мс")
            return True
        except Exception as e:
            print(f"[Руки] Ошибка запуска встроенной утилиты '{exe}': {e}")

    # 2. Поиск в кэше ярлыков
    shortcuts = index_shortcuts() if PROGRAM_CACHE_ENABLED else index_shortcuts(force=True)

    def search_in_list(target_name, items):
        exact = []
        partial = []
        for s_lower, full_path, orig_name in items:
            if target_name == s_lower:
                exact.append((full_path, orig_name))
            elif target_name in s_lower:
                partial.append((full_path, orig_name))
        if exact:
            return exact[0]
        if partial:
            partial.sort(key=lambda x: len(x[1]))
            return partial[0]
        return None

    match = search_in_list(name, shortcuts)

    # 3. Fallback при промахе кэша: если программа не найдена, обновляем индекс один раз
    if not match and PROGRAM_CACHE_ENABLED:
        shortcuts = index_shortcuts(force=True)
        match = search_in_list(name, shortcuts)

    if match:
        path, original_name = match
        try:
            os.startfile(path)
            if PROFILING_ENABLED:
                print(f"[Руки] Запуск ярлыка '{original_name}' за {(time.perf_counter() - t_start)*1000:.2f} мс")
            else:
                print(f"[Руки] Выполнено: Запуск ярлыка '{original_name}'")
            return True
        except Exception as e:
            print(f"[Руки] Ошибка запуска ярлыка '{original_name}': {e}")
            return False

    # 4. Безопасный запуск прямых ASCII-команд (например, если передано 'notepad' или 'calc')
    # Исключаем вызов cmd.exe для русских названий во избежание ложных ошибок
    if name.isascii() and " " not in name:
        try:
            subprocess.Popen([name], shell=False)
            print(f"[Руки] Выполнено: Прямой запуск команды '{name}'")
            return True
        except Exception:
            pass

    print(f"[Руки] Не удалось найти или запустить программу '{name}' на вашем ПК.")
    return False


def search_web(query):
    """Открывает браузер по умолчанию со страницей поиска Google."""
    if not query:
        print("[Руки] Пустой поисковый запрос.")
        return False

    t_start = time.perf_counter()
    url = f"https://www.google.com/search?q={query}"
    try:
        webbrowser.open(url)
        if PROFILING_ENABLED:
            print(f"[Руки] Поиск в Google: '{query}' (открытие за {(time.perf_counter() - t_start)*1000:.2f} мс)")
        else:
            print(f"[Руки] Выполнено: Поиск в Google: '{query}'")
        return True
    except Exception as e:
        print(f"[Руки] Ошибка при открытии браузера: {e}")
        return False


def handle_command(text):
    """Анализирует команду через ИИ/FastPath и выполняет действие."""
    t_start = time.perf_counter()
    ai_result = parse_command_with_ai(text)

    action = ai_result.get("action", "unknown")
    program_key = ai_result.get("program_key", "")
    query = ai_result.get("query", "")

    if action == "open_program":
        find_and_run_program(program_key)
    elif action == "search_web":
        search_web(query)
    elif action == "unknown":
        search_query = query if query else text
        search_web(search_query)

    # Оконные команды ("hide", "exit") возвращаются в aurora.py для обработки в GUI
    return ai_result
