from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Базовый класс для всех SQLAlchemy моделей.
    В будущем сюда можно добавить общие поля (например, id, created_at).
    """
