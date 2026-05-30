from app import app, ensure_database_schema


if __name__ == "__main__":
    with app.app_context():
        ensure_database_schema()

    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
