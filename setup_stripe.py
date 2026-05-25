"""
Creates the 3 Leado subscription products and prices in Stripe.
Run once before deploying:  python3 setup_stripe.py
"""

import os
import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

PLANS = [
    {"name": "Starter", "amount": 999,  "env": "STRIPE_PRICE_STARTER"},
    {"name": "Pro",     "amount": 2999, "env": "STRIPE_PRICE_PRO"},
    {"name": "Agency",  "amount": 7999, "env": "STRIPE_PRICE_AGENCY"},
]

print("\nLeado — Stripe setup\n" + "─" * 40)

results = {}

for plan in PLANS:
    product = stripe.Product.create(
        name=f"Leado {plan['name']}",
        description=f"Leado {plan['name']} — abonnement mensuel",
    )
    price = stripe.Price.create(
        product=product.id,
        unit_amount=plan["amount"],
        currency="eur",
        recurring={"interval": "month"},
    )
    results[plan["env"]] = price.id
    euros = plan["amount"] / 100
    print(f"  ✓ {plan['name']:8s}  {euros:.2f}€/mois  →  {price.id}")

print("\n" + "─" * 40)
print("Copie ces lignes dans ton .env et dans Render :\n")
for env_key, price_id in results.items():
    print(f"{env_key}={price_id}")

print()
