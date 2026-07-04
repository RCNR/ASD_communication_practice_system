from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ASD Communication Practice System"
    DEBUG: bool = False
    OPENAI_API_KEY: str = "${OPENAI_API_KEY}"
    OPENAI_MODEL: str = "gpt-4o-mini"
    SECRET_KEY: str = "dev-secret-change-me"

    class Config:
        env_file = ".env"


settings = Settings()
