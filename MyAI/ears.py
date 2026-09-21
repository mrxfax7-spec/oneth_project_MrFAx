import time
import speech_recognition as sr
from config import (
    LISTEN_TIMEOUT,
    PHRASE_TIME_LIMIT,
    COMMAND_PAUSE_THRESHOLD,
    PROFILING_ENABLED,
    STT_ERROR_BACKOFF,
)

# Инициализируем распознаватель речи и микрофон
recognizer = sr.Recognizer()
microphone = sr.Microphone()

# Настройки чувствительности и порогов тишины
recognizer.dynamic_energy_threshold = True
recognizer.dynamic_energy_adjustment_damping = 0.15
recognizer.non_speaking_duration = 0.4  # Уменьшаем захват лишней тишины

_last_network_error_log = 0.0


def init_ears():
    """Калибровка микрофона под уровень фонового шума."""
    print("[*] Калибровка микрофона... Пожалуйста, помолчите полсекунды.")
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.6)
    print("[*] Калибровка завершена. Аврора готова слушать.")


def listen(pause_threshold=COMMAND_PAUSE_THRESHOLD):
    """Слушает микрофон и возвращает распознанный текст на русском языке с замером времени."""
    recognizer.pause_threshold = pause_threshold

    with microphone as source:
        try:
            t0_listen = time.perf_counter()
            # Захватываем аудио из микрофона с учетом таймаутов
            audio = recognizer.listen(
                source,
                timeout=LISTEN_TIMEOUT,
                phrase_time_limit=PHRASE_TIME_LIMIT,
            )
            t_listen = time.perf_counter() - t0_listen

            t0_stt = time.perf_counter()
            # Отправляем аудио в Google Speech Recognition
            text = recognizer.recognize_google(audio, language="ru-RU")
            t_stt = time.perf_counter() - t0_stt

            clean_text = text.strip().lower()

            if PROFILING_ENABLED and clean_text:
                print(f"[СЛУХ] Запись: {t_listen:.2f}с | Распознавание STT: {t_stt:.2f}с")

            return clean_text

        except sr.WaitTimeoutError:
            # Таймаут тишины
            return ""
        except sr.UnknownValueError:
            # Звук был, но речь не распознана
            return ""
        except sr.RequestError as e:
            # Сетевая ошибка сервиса Google STT (DNS, сброс соединения, нет интернета)
            global _last_network_error_log
            now = time.time()
            if now - _last_network_error_log > 5.0:
                print(f"[!] Сеть: сервис распознавания речи временно недоступен ({e}). Повтор через {STT_ERROR_BACKOFF}с...")
                _last_network_error_log = now
            return None
        except Exception as e:
            print(f"[!] Непредвиденная ошибка аудио: {e}")
            return ""
