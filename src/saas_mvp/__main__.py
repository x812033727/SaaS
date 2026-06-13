"""Entry point: `python -m saas_mvp`"""

import uvicorn

from saas_mvp.config import settings


def main() -> None:
    """Start the server — also used as the console_script entry point."""
    uvicorn.run(
        "saas_mvp.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
