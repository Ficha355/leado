import os
import logging
import stripe
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from db import db, User, Search, Lead
from scraper import run_pipeline

logging.basicConfig(level=logging.INFO)

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL") or "sqlite:///leado.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Connecte-toi pour accéder à ton dashboard."

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

PLANS = {
    "starter": {
        "name": "Starter",
        "price": "9,99€",
        "price_id": os.environ.get("STRIPE_PRICE_STARTER", ""),
        "searches": 100,
    },
    "pro": {
        "name": "Pro",
        "price": "29,99€",
        "price_id": os.environ.get("STRIPE_PRICE_PRO", ""),
        "searches": 500,
    },
    "agency": {
        "name": "Agency",
        "price": "79,99€",
        "price_id": os.environ.get("STRIPE_PRICE_AGENCY", ""),
        "searches": -1,
    },
}


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        if User.query.filter_by(email=email).first():
            flash("Cet email est déjà utilisé.", "error")
            return redirect(url_for("register"))
        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            full_name=full_name,
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Email ou mot de passe incorrect.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


# ── Protected routes ──────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    recent_searches = current_user.searches.order_by(Search.created_at.desc()).limit(10).all()
    hot_leads = (
        Lead.query.filter_by(user_id=current_user.id, intent_label="hot")
        .order_by(Lead.created_at.desc())
        .limit(20)
        .all()
    )
    stats = {
        "total_searches": current_user.searches.count(),
        "total_leads": current_user.leads.count(),
        "hot_leads": current_user.leads.filter_by(intent_label="hot").count(),
        "saved_leads": current_user.leads.filter_by(is_saved=True).count(),
    }
    return render_template("dashboard.html", searches=recent_searches, hot_leads=hot_leads, stats=stats)


@app.route("/search", methods=["POST"])
@login_required
def run_search():
    data = request.get_json(silent=True) or {}
    product = data.get("query", "").strip()
    sources = data.get("sources", ["reddit", "youtube"])

    if not product:
        return jsonify({"error": "Décris ce que tu vends."}), 400

    # Enforce plan limits
    used = current_user.searches.count()
    limit = current_user.monthly_search_limit
    if limit != 99999 and used >= limit:
        return jsonify({"error": f"Limite mensuelle atteinte ({limit} recherches). Upgrade ton plan."}), 403

    search = Search(user_id=current_user.id, query=product, sources=sources, status="running")
    db.session.add(search)
    db.session.commit()

    try:
        raw_leads = run_pipeline(product, sources)
    except Exception as exc:
        search.status = "failed"
        db.session.commit()
        return jsonify({"error": f"Erreur pipeline : {exc}"}), 500

    saved_leads = []
    for rl in raw_leads:
        lead = Lead(
            user_id=current_user.id,
            search_id=search.id,
            source=rl["source"],
            source_url=rl.get("source_url"),
            author=rl.get("author"),
            content_snippet=rl.get("content_snippet"),
            translated_snippet=rl.get("translated_snippet"),
            intent_score=rl.get("intent_score", 0) / 100,  # store as 0.0–1.0
            intent_label=rl.get("intent_label", "cold"),
            ai_summary=rl.get("ai_summary"),
            suggested_reply=rl.get("suggested_reply"),
        )
        db.session.add(lead)
        saved_leads.append(lead)

    search.status = "done"
    search.result_count = len(saved_leads)
    db.session.commit()

    return jsonify({
        "search_id": search.id,
        "count": len(saved_leads),
        "leads": [_lead_to_dict(l) for l in saved_leads],
    })


def _lead_to_dict(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "source": lead.source,
        "source_url": lead.source_url,
        "author": lead.author,
        "content_snippet": lead.content_snippet,
        "translated_snippet": lead.translated_snippet,
        "intent_score": round((lead.intent_score or 0) * 100),
        "intent_label": lead.intent_label,
        "ai_summary": lead.ai_summary,
        "suggested_reply": lead.suggested_reply,
        "is_saved": lead.is_saved,
        "is_contacted": lead.is_contacted,
    }


@app.route("/leads")
@login_required
def leads():
    label = request.args.get("label")
    source = request.args.get("source")
    q = Lead.query.filter_by(user_id=current_user.id)
    if label:
        q = q.filter_by(intent_label=label)
    if source:
        q = q.filter_by(source=source)
    results = q.order_by(Lead.intent_score.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=25
    )
    return render_template("leads.html", leads=results)


@app.route("/api/leads/<int:search_id>")
@login_required
def api_leads_by_search(search_id):
    search = Search.query.filter_by(id=search_id, user_id=current_user.id).first_or_404()
    leads = Lead.query.filter_by(search_id=search_id).order_by(Lead.intent_score.desc()).all()
    return jsonify({
        "search_id": search_id,
        "status": search.status,
        "count": len(leads),
        "leads": [_lead_to_dict(l) for l in leads],
    })


@app.route("/leads/<int:lead_id>/save", methods=["POST"])
@login_required
def save_lead(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    lead.is_saved = not lead.is_saved
    db.session.commit()
    return jsonify({"saved": lead.is_saved})


@app.route("/leads/<int:lead_id>/contact", methods=["POST"])
@login_required
def mark_contacted(lead_id):
    lead = Lead.query.filter_by(id=lead_id, user_id=current_user.id).first_or_404()
    lead.is_contacted = True
    db.session.commit()
    return jsonify({"contacted": True})


# ── Billing ───────────────────────────────────────────────────────────────────

@app.route("/checkout/<plan_key>")
@login_required
def checkout(plan_key):
    plan = PLANS.get(plan_key)
    if not plan:
        return redirect(url_for("index"))
    if not plan["price_id"]:
        flash("Stripe non configuré.", "error")
        return redirect(url_for("dashboard"))
    session_obj = stripe.checkout.Session.create(
        customer_email=current_user.email,
        payment_method_types=["card"],
        line_items=[{"price": plan["price_id"], "quantity": 1}],
        mode="subscription",
        success_url=url_for("billing_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("index", _external=True) + "#pricing",
        metadata={"user_id": current_user.id, "plan": plan_key},
    )
    return redirect(session_obj.url)


@app.route("/billing/success")
@login_required
def billing_success():
    flash("Abonnement activé ! Bienvenue sur Leado.", "success")
    return redirect(url_for("dashboard"))


@app.route("/billing/portal")
@login_required
def billing_portal():
    if not current_user.stripe_customer_id:
        return redirect(url_for("dashboard"))
    portal = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=url_for("dashboard", _external=True),
    )
    return redirect(portal.url)


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        user_id = int(s["metadata"]["user_id"])
        plan_key = s["metadata"]["plan"]
        user = db.session.get(User, user_id)
        if user:
            user.plan = plan_key
            user.stripe_customer_id = s.get("customer")
            user.stripe_subscription_id = s.get("subscription")
            db.session.commit()

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub = event["data"]["object"]
        user = User.query.filter_by(stripe_subscription_id=sub["id"]).first()
        if user:
            user.plan = "free"
            db.session.commit()

    return "", 200


# ── Dev helpers (REMOVE before prod) ─────────────────────────────────────────

@app.route("/dev/create-admin")
def dev_create_admin():
    existing = User.query.filter_by(email="admin@leado.io").first()
    if existing:
        return jsonify({"status": "already exists", "email": existing.email, "plan": existing.plan})
    user = User(
        email="admin@leado.io",
        password_hash=generate_password_hash("leado2025"),
        full_name="Admin",
        plan="agency",
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({"status": "created", "email": user.email, "plan": user.plan, "search_limit": user.monthly_search_limit})


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify({
        "plan": current_user.plan,
        "searches_used": current_user.searches.count(),
        "searches_limit": current_user.monthly_search_limit,
        "leads_total": current_user.leads.count(),
    })


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
