from flask_sqlalchemy import SQLAlchemy


# Type annotation for db instance - this is just for mypy type checking
# The actual db instance is imported from app.ext
db: SQLAlchemy

# Export db for type annotations in other models
__all__ = ["db"]
