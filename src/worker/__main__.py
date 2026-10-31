"""Run one worker process: PYTHONPATH=src python -m worker."""
import uvicorn

from worker.config import Settings
from worker.main import create_app


def main():
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
