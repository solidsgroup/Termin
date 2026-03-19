from startup import app, prepare_runtime


if __name__ == "__main__":
    prepare_runtime()
    app.run(debug=True)
