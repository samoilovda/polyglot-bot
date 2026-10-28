# Помощник учебного чата (chat_assistant.py) для stelve; публикации в канал (main.py) остаются в GitHub Actions.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-chat.txt .
RUN pip install --no-cache-dir -r requirements-chat.txt && useradd --system --uid 10001 app
COPY chat_assistant.py .
USER app
CMD ["python", "chat_assistant.py"]
