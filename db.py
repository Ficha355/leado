from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255))
    plan = db.Column(db.String(50), default="free")  # free | starter | pro | agency
    stripe_customer_id = db.Column(db.String(255), unique=True)
    stripe_subscription_id = db.Column(db.String(255), unique=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    searches = db.relationship("Search", backref="user", lazy="dynamic")
    leads = db.relationship("Lead", backref="user", lazy="dynamic")

    @property
    def monthly_search_limit(self):
        limits = {"free": 5, "starter": 100, "pro": 500, "agency": 99999}
        return limits.get(self.plan, 5)


class Search(db.Model):
    __tablename__ = "searches"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    query = db.Column(db.String(500), nullable=False)
    sources = db.Column(db.JSON, default=["reddit", "youtube", "google"])
    status = db.Column(db.String(50), default="pending")  # pending | running | done | failed
    result_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    leads = db.relationship("Lead", backref="search", lazy="dynamic")


class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    search_id = db.Column(db.Integer, db.ForeignKey("searches.id"), nullable=False)
    source = db.Column(db.String(50))  # reddit | youtube | google
    source_url = db.Column(db.Text)
    author = db.Column(db.String(255))
    content_snippet = db.Column(db.Text)
    translated_snippet = db.Column(db.Text)  # French translation if original is English
    intent_score = db.Column(db.Float)  # 0.0 - 1.0, scored by Claude
    intent_label = db.Column(db.String(50))  # hot | warm | cold
    ai_summary = db.Column(db.Text)
    suggested_reply = db.Column(db.Text)
    is_saved = db.Column(db.Boolean, default=False)
    is_contacted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (db.Index("ix_leads_intent_score", "intent_score"),)
