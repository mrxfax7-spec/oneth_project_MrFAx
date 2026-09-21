import os
import sys

def setup_startup():
    print("=" * 50)
    print("  Настройка автозапуска ассистента «Аврора»")
    print("=" * 50)

    # Путь к папке автозагрузки Windows
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("[Ошибка] Не удалось найти переменную окружения APPDATA.")
        return

    startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    if not os.path.exists(startup_dir):
        print(f"[Ошибка] Папка автозагрузки не найдена: {startup_dir}")
        return

    bat_path = os.path.join(startup_dir, "aurora_start.bat")
    
    # Путь к директории проекта (получаем короткий ASCII-путь 8.3 для совместимости с cmd.exe)
    long_project_dir = "C:\\личное\\утилиты\\MyAI"
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.kernel32.GetShortPathNameW(long_project_dir, buf, 260)
        project_dir = buf.value
        if not project_dir:
            project_dir = long_project_dir
    except Exception:
        project_dir = long_project_dir

    python_exe = sys.executable

    # Содержимое бат-файла (теперь полностью ASCII, никаких кодировочных проблем!)
    bat_content = f"""@echo off
cd /d "{project_dir}"
start "" "{python_exe}" aurora.py
"""

    try:
        # Пишем в обычной utf-8/ascii кодировке
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        print(f"[Успех] Файл автозапуска создан: {bat_path}")
        print(f"[Инфо] Теперь фоновая служба Авроры будет автоматически запускаться при старте Windows.")
        print(f"\nВы можете проверить или запустить её прямо сейчас, выполнив этот бат-файл.")
    except Exception as e:
        print(f"[Ошибка] Не удалось записать файл автозапуска: {e}")

if __name__ == "__main__":
    setup_startup()
