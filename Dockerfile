# ==========================================
# Этап 1: Сборка зависимостей с помощью uv (Python 3.14)
# ==========================================
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# 1. Копируем файлы управления зависимостями
COPY pyproject.toml uv.lock ./

# 2. Создаем venv и ставим зависимости
RUN uv sync --frozen --no-install-project

# 3. Копируем весь исходный код
COPY . .

# 4. Устанавливаем сам проект
RUN uv sync --frozen

# ==========================================
# Этап 2: Минимальный runtime-образ (Python 3.14)
# ==========================================
FROM python:3.14-slim

WORKDIR /app

# Копируем готовое виртуальное окружение из билдера
COPY --from=builder /app/.venv /app/.venv

# Копируем содержимое src/app напрямую в /app/app
# Это решает проблему с src layout - Python будет находить модуль app
COPY --from=builder /app/src/app /app/app

# Отключаем буферизацию вывода для мгновенных логов
ENV PYTHONUNBUFFERED=1

# Запускаем через абсолютный путь к python из venv
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]