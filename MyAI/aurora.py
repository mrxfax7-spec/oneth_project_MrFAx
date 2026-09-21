import sys
import os
import time
import ctypes
import threading
from datetime import datetime

# Импортируем элементы PyQt5 для работы с треем и сигналами
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QStyle
from PyQt5.QtCore import QObject, pyqtSignal, QCoreApplication

# Импортируем наши модули и настройки
from config import (
    WAKE_WORDS,
    LISTEN_COOLDOWN,
    COMMAND_PAUSE_THRESHOLD,
    WAKE_WORD_PAUSE_THRESHOLD,
    PROFILING_ENABLED,
    STT_ERROR_BACKOFF,
)
from ears import init_ears, listen
from hands import handle_command, index_shortcuts
from brain import warmup_ollama

# Глаголы действий, требующие дополнения (программы или запроса)
ACTION_VERBS = frozenset([
    "открой", "запусти", "включи", "найди", "поищи", "открыть", "запустить", "поиск",
    "open", "launch", "run", "find", "search"
])

# Вспомогательные слова: междометия, вежливые формы, предлоги и контекст поиска
FILLER_WORDS = frozenset([
    "пожалуйста", "плиз", "эээ", "ммм", "ну", "мне", "нам",
    "программу", "приложение", "утилиту", "в", "на", "интернете", "интернет",
    "гугле", "гугл", "яндексе", "яндекс", "браузере", "браузер", "сети", "сеть",
    "быстро", "скорее", "please", "app", "the", "a", "an"
])


def is_incomplete_command(text: str) -> bool:
    """
    Проверяет, является ли фраза незавершённым началом команды без конкретного объекта.
    Например:
    - 'открой', 'открой пожалуйста', 'запусти эээ', 'найди в интернете' -> True
    - 'открой Discord', 'найди рецепт пиццы', 'свернись', 'выключись' -> False
    """
    clean = text.lower().strip(" ,.?!:;«»-")
    words = clean.split()
    if not words:
        return False

    first_word = words[0]
    if first_word not in ACTION_VERBS:
        return False

    # Проверяем слова после глагола действия
    tail_words = words[1:]
    substantive_words = [w for w in tail_words if w not in FILLER_WORDS]

    # Если после фильтрации слов-заполнителей ничего не осталось — команда не завершена
    return len(substantive_words) == 0


def extract_command_after_wake_word(text: str, wake_words: list) -> tuple:
    """
    Ищет триггерное слово в фразе и возвращает (найденный_триггер, остаток_команды).
    Если остатка нет, возвращает (найденный_триггер, "").
    """
    clean_text = text.lower().strip(" ,.?!:;«»-")
    sorted_wake_words = sorted(wake_words, key=len, reverse=True)

    for word in sorted_wake_words:
        idx = clean_text.find(word)
        if idx != -1:
            before_ok = (idx == 0 or not clean_text[idx - 1].isalnum())
            after_idx = idx + len(word)
            after_ok = (after_idx == len(clean_text) or not clean_text[after_idx].isalnum())

            if before_ok and after_ok:
                remainder = (clean_text[:idx] + " " + clean_text[after_idx:]).strip(" ,.?!:;«»-")
                remainder = " ".join(remainder.split())
                return word, remainder

    return None, ""


class AuroraApp(QObject):
    # Определяем Qt-сигналы для безопасного межпоточного взаимодействия
    show_console_signal = pyqtSignal()
    hide_console_signal = pyqtSignal()
    exit_app_signal = pyqtSignal()
    notification_signal = pyqtSignal(str, str)

    def __init__(self, qt_app):
        super().__init__()
        self.qt_app = qt_app
        self.state = "COMMAND"  # Начинаем в активном режиме приема команд
        self.hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        self.is_running = True
        self.empty_count = 0

        # Связываем сигналы со слотами (методами) в главном потоке GUI
        self.show_console_signal.connect(self.show_console)
        self.hide_console_signal.connect(self.hide_console)
        self.exit_app_signal.connect(self.exit_app)
        self.notification_signal.connect(self.show_notification)

        # Создаем иконку в системном трее
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.tray_icon = QSystemTrayIcon(self.qt_app.style().standardIcon(QStyle.SP_ComputerIcon), self)
        self.tray_icon.setToolTip("Аврора — Голосовой ассистент")

        # Контекстное меню для трея
        menu = QMenu()
        show_action = QAction("Открыть консоль", menu)
        show_action.triggered.connect(self.show_console)

        exit_action = QAction("Выход из Авроры", menu)
        exit_action.triggered.connect(self.exit_app)

        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)

        # Восстановление консоли по двойному клику
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

        # Фоновый рабочий поток
        self.listen_thread = threading.Thread(target=self.listen_loop, daemon=True, name="AuroraListenLoop")
        self.listen_thread.start()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_console_signal.emit()

    def show_console(self):
        """Показывает окно консоли (главный поток GUI)."""
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 5)  # 5 = SW_SHOW
            ctypes.windll.user32.SetForegroundWindow(self.hwnd)
        self.state = "COMMAND"
        self.empty_count = 0
        print("\n[Аврора] Окно развернуто. Слушаю команду...")

    def hide_console(self):
        """Скрывает окно консоли (главный поток GUI)."""
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 0)  # 0 = SW_HIDE
        self.state = "WAKE_WORD"
        self.empty_count = 0

    def show_notification(self, title, message):
        """Выводит системное уведомление Windows (главный поток GUI)."""
        try:
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 2500)
        except Exception:
            pass

    def exit_app(self):
        """Полный выход из приложения (главный поток GUI)."""
        print("\n[Аврора] Завершение работы...")
        self.is_running = False
        self.tray_icon.hide()
        QCoreApplication.quit()
        os._exit(0)

    def process_command(self, heard):
        """Обрабатывает полученную команду пользователя."""
        self.empty_count = 0
        print(f"\n[Слух] Услышано: «{heard}»")

        # 2. Дозапись неполных команд
        if is_incomplete_command(heard):
            print(f"[Аврора] Слышу начало команды «{heard}». Жду продолжение...")
            second_part = listen(pause_threshold=COMMAND_PAUSE_THRESHOLD)
            if second_part:
                heard = f"{heard} {second_part}".strip()
                print(f"[Слух] Полная сформированная команда: «{heard}»")

        # Замеряем полный цикл обработки команды
        t0_cmd = time.perf_counter()
        ai_result = handle_command(heard)
        t_total = time.perf_counter() - t0_cmd

        if PROFILING_ENABLED:
            print(f"[ИТОГО] Обработка команды завершена за {t_total*1000:.2f} мс ({t_total:.3f}с)")

        action = ai_result.get("action", "")
        if action == "exit":
            self.exit_app_signal.emit()
            return "exit"
        elif action == "hide":
            self.hide_console_signal.emit()
            self.notification_signal.emit("Аврора свернулась", "Консоль скрыта. Я продолжаю слушать вас в фоне.")
            return "hide"

        print("\n[Аврора] Жду следующую команду...")
        return "ok"

    def listen_loop(self):
        """Основной цикл работы ассистента в фоновом потоке."""
        print("=" * 50)
        print("     А В Р О Р А  —  Голосовой ассистент в трее")
        print("=" * 50)
        print(f"  Триггеры для вызова : {', '.join(WAKE_WORDS)}")
        print(f"  Время старта        : {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 50)
        print()

        # Калибровка микрофона
        try:
            init_ears()
        except Exception as e:
            print(f"[Критическая ошибка] Не удалось инициализировать микрофон: {e}")
            self.notification_signal.emit("Аврора: Ошибка", "Не удалось инициализировать микрофон!")
            time.sleep(1)
            self.exit_app_signal.emit()
            return

        # Запускаем фоновый прогрев модели и индексацию ярлыков (не блокирует речь!)
        warmup_ollama()
        threading.Thread(target=index_shortcuts, daemon=True, name="PreIndexShortcuts").start()

        print("\n[Аврора] Готова к работе. Слушаю команду...")

        while self.is_running:
            time.sleep(LISTEN_COOLDOWN)

            if self.state == "WAKE_WORD":
                heard = listen(pause_threshold=WAKE_WORD_PAUSE_THRESHOLD)

                # 3. Сетевая ошибка Google STT: пауза без спама
                if heard is None:
                    time.sleep(STT_ERROR_BACKOFF)
                    continue

                if not heard:
                    continue

                matched_wake_word, command_remainder = extract_command_after_wake_word(heard, WAKE_WORDS)

                if matched_wake_word:
                    # Позвали Аврору! Разворачиваем консоль
                    self.show_console_signal.emit()
                    self.state = "COMMAND"

                    try:
                        ctypes.windll.user32.MessageBeep(0)
                    except Exception:
                        pass

                    if command_remainder:
                        # 1. Команда произнесена слитно с именем: "Аврора, открой Discord"
                        res = self.process_command(command_remainder)
                        if res == "exit":
                            break
                    else:
                        # Сказано только имя: "Аврора" -> переходим в режим ожидания команды
                        self.notification_signal.emit("Аврора", "Слушаю вас!")

            elif self.state == "COMMAND":
                heard = listen(pause_threshold=COMMAND_PAUSE_THRESHOLD)

                # 3. Сетевая ошибка Google STT: пауза, не сворачиваемся в трей
                if heard is None:
                    time.sleep(STT_ERROR_BACKOFF)
                    continue

                if not heard:
                    self.empty_count += 1
                    if self.empty_count >= 3:
                        print("\n[Аврора] Нет активности. Сворачиваюсь в трей.")
                        self.hide_console_signal.emit()
                        self.notification_signal.emit("Аврора свернулась", "Я ушла в трей. Скажите «Аврора», чтобы вернуть меня.")
                    else:
                        print(f"[~] Ожидание команды ({self.empty_count}/3)...", end="\r", flush=True)
                    continue

                res = self.process_command(heard)
                if res == "exit":
                    break

    def run(self):
        sys.exit(self.qt_app.exec_())


if __name__ == "__main__":
    qt_app = QApplication(sys.argv)
    app = AuroraApp(qt_app)
    app.run()
