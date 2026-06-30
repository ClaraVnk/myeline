import secrets

from app.ext import db
from app.utils.time_utils import utcnow


class SharedRagResult(db.Model):
    __tablename__ = "shared_rag_results"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True,
                      default=lambda: secrets.token_urlsafe(32))
    query = db.Column(db.String(500), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    sources_json = db.Column(db.Text, nullable=True)
    confidence = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    @classmethod
    def create(cls, query: str, answer: str, sources: list, confidence: int) -> "SharedRagResult":
        import json
        obj = cls(
            query=query,
            answer=answer,
            sources_json=json.dumps(sources, ensure_ascii=False),
            confidence=confidence,
        )
        db.session.add(obj)
        db.session.flush()
        return obj

    @property
    def sources(self) -> list:
        import json
        if not self.sources_json:
            return []
        return json.loads(self.sources_json)
