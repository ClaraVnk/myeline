import os


try:
    from dotenv import load_dotenv

    load_dotenv()
    print("✅ Variables .env chargées")
except ImportError:
    print("❌ python-dotenv non installé")

from app import create_app


application = create_app()

if __name__ == "__main__":
    debug_env = os.getenv("FLASK_DEBUG", "False").lower()
    debug = debug_env in ("true", "1", "t")
    port = int(os.getenv("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=debug)  # nosec B104
