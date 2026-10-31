import uvicorn
from controller.config import Settings
from controller.main import create_app


if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
