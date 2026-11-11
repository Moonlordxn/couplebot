import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets" / "pets"
logger = logging.getLogger(__name__)

# Поддерживаем оба варианта размещения проекта:
# /app/.env и /app/couplepet/.env.
# Переменные, заданные в панели хостинга, имеют приоритет над файлом.
try:
    from dotenv import load_dotenv

    env_candidates = (
        BASE_DIR.parent / ".env",
        BASE_DIR / ".env",
    )
    for env_path in env_candidates:
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            logger.info("Найден файл конфигурации: %s", env_path)
except ImportError:
    logger.warning("python-dotenv не установлен; используются переменные окружения хостинга")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "couplepet.db"))

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Проверьте переменную BOT_TOKEN в панели хостинга "
        "или файл /app/.env. Перезапустите приложение после изменения настроек."
    )


def _parse_admin_ids() -> set[int]:
    raw_values = [
        os.getenv("OWNER_ID", ""),
        os.getenv("ADMIN_IDS", ""),
    ]
    result: set[int] = set()
    for raw in raw_values:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                value = int(item)
                if value > 0:
                    result.add(value)
                else:
                    logger.error("ID администратора должен быть положительным числом")
            except ValueError:
                logger.error("Некорректное значение в OWNER_ID/ADMIN_IDS: %r", item)
    return result


ADMIN_IDS = _parse_admin_ids()
OWNER_ID = min(ADMIN_IDS) if ADMIN_IDS else 0

# Диагностика без вывода токена или других секретов.
logger.info(
    "Конфигурация загружена: BOT_TOKEN задан=%s, ADMIN_IDS=%s",
    bool(BOT_TOKEN),
    sorted(ADMIN_IDS),
)

# Настройки игры
MAX_FAMILY_MEMBERS = 2
PET_ACTION_COOLDOWN = 5  # секунд
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@couplepets_updates").strip()
