from sqlalchemy import inspect, select, text

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password
from app.settings import get_settings


def main() -> None:
    Base.metadata.create_all(engine)
    # This small project deliberately avoids a migration dependency. Keep the
    # one additive schema upgrade needed by existing local test installations.
    columns = {column["name"] for column in inspect(engine).get_columns("deliveries")}
    if "platform_options" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE deliveries ADD COLUMN platform_options JSON"))
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    with SessionLocal.begin() as session:
        email = settings.bootstrap_admin_email.lower()
        if session.scalar(select(User.id).where(User.email == email)) is None:
            session.add(
                User(
                    email=email,
                    password_hash=hash_password(settings.bootstrap_admin_password.get_secret_value()),
                    display_name="Owner",
                    is_superuser=True,
                )
            )


if __name__ == "__main__":
    main()
