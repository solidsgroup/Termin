import argparse

from startup import app, prepare_runtime, socketio


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Termin development server.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode and server-side debug console messages.",
    )
    args = parser.parse_args()

    app.config["TERMIN_DEBUG_CONSOLE"] = bool(args.debug)
    prepare_runtime()
    if args.debug:
        print("Server debug console enabled.")
    socketio.run(app, debug=bool(args.debug))
