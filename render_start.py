import os
import sys

from startup import prepare_runtime


def main() -> None:
    prepare_runtime(interactive=False)
    bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
    workers = os.getenv("WEB_CONCURRENCY", "1")
    threads = os.getenv("GUNICORN_THREADS", "8")
    timeout = os.getenv("GUNICORN_TIMEOUT", "120")
    os.execvp(
        "gunicorn",
        [
            "gunicorn",
            "--bind",
            bind,
            "--worker-class",
            "gthread",
            "--workers",
            workers,
            "--threads",
            threads,
            "--timeout",
            timeout,
            "wsgi:app",
        ],
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
