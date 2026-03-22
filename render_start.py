import os
import sys

from startup import app, prepare_runtime, socketio


def main() -> None:
    prepare_runtime(interactive=False)
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "10000"))
    socketio.run(app, host=host, port=port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
