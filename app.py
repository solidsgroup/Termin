from startup import app, prepare_runtime, socketio


if __name__ == "__main__":
    prepare_runtime()
    socketio.run(app, debug=True)
