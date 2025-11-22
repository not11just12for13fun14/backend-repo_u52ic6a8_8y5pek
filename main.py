import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime, timezone

import stripe

from database import create_document, get_documents, db
from schemas import Session, Message, Idea, Account

app = FastAPI(title="Thinking Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_YEARLY = os.getenv("STRIPE_PRICE_YEARLY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:3000?status=success")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "http://localhost:3000?status=cancel")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI Backend!"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# ---------- Thinking Assistant Logic ----------
Category = Literal["business", "content", "general"]
Plan = Literal["free", "pro"]


class InitAccountPayload(BaseModel):
    client_id: str = Field(..., min_length=6)
    email: Optional[str] = None


class UpgradePayload(BaseModel):
    client_id: str = Field(..., min_length=6)


class StartSessionPayload(BaseModel):
    category: Category = Field(..., description="Type of brainstorming")
    name: Optional[str] = Field(None, description="Optional user name or alias")
    goal: Optional[str] = Field(None, description="One-line goal or theme for this session")
    client_id: str = Field(..., description="Anonymous client identifier")


class AnswerPayload(BaseModel):
    answer: str = Field(..., min_length=1)


class CreateCheckoutPayload(BaseModel):
    client_id: str
    price_id: Optional[str] = None
    interval: Optional[str] = Field(None, description="monthly or yearly")
    email: Optional[str] = None


class BillingPortalPayload(BaseModel):
    client_id: str


QUESTION_BANK: Dict[Category, List[str]] = {
    "business": [
        "What problem do you want to solve?",
        "Who exactly has this problem (your target user)?",
        "How are they solving it today?",
        "What unique strengths or resources do you have?",
        "What constraints do you have (time, budget, skills)?",
    ],
    "content": [
        "What topic or niche are you most excited about?",
        "Who is your ideal audience?",
        "What formats do you enjoy creating (video, writing, audio, etc.)?",
        "How often can you realistically publish?",
        "What platforms do you want to prioritize?",
    ],
    "general": [
        "Describe the challenge in one sentence.",
        "What would a great outcome look like?",
        "What is the biggest obstacle right now?",
        "What resources or help do you have access to?",
        "What is the next smallest step you could take?",
    ],
}


def account_collection_name() -> str:
    return Account.__name__.lower()


def session_collection_name() -> str:
    return Session.__name__.lower()


def message_collection_name() -> str:
    return Message.__name__.lower()


def idea_collection_name() -> str:
    return Idea.__name__.lower()


# ---- Account/Plan Endpoints ----
@app.post("/api/account/init")
def init_account(payload: InitAccountPayload):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    col = db[account_collection_name()]
    existing = col.find_one({"client_id": payload.client_id})
    now = datetime.now(timezone.utc)
    if existing:
        update: Dict[str, Any] = {"updated_at": now}
        if payload.email and not existing.get("email"):
            update["email"] = payload.email
        if update:
            col.update_one({"client_id": payload.client_id}, {"$set": update})
        return {"client_id": existing.get("client_id"), "plan": existing.get("plan", "free")}

    acc = Account(client_id=payload.client_id, created_at=now, updated_at=now, email=payload.email)
    create_document(account_collection_name(), acc)
    return {"client_id": payload.client_id, "plan": "free"}


@app.post("/api/account/upgrade")
def upgrade_account(payload: UpgradePayload):
    # Legacy mock upgrade for demo
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    result = db[account_collection_name()].update_one(
        {"client_id": payload.client_id}, {"$set": {"plan": "pro", "updated_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        # create if not exists
        acc = Account(client_id=payload.client_id, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), plan="pro")
        create_document(account_collection_name(), acc)
    return {"client_id": payload.client_id, "plan": "pro"}


# ---- Billing (Stripe) ----
@app.post("/api/billing/checkout")
def create_checkout_session(payload: CreateCheckoutPayload):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    col = db[account_collection_name()]
    acct = col.find_one({"client_id": payload.client_id})
    now = datetime.now(timezone.utc)
    if not acct:
        acc = Account(client_id=payload.client_id, created_at=now, updated_at=now, email=payload.email)
        create_document(account_collection_name(), acc)
        acct = col.find_one({"client_id": payload.client_id})

    # Ensure customer exists
    customer_id = acct.get("stripe_customer_id")
    email = payload.email or acct.get("email")
    if not customer_id:
        customer = stripe.Customer.create(email=email, metadata={"client_id": payload.client_id})
        customer_id = customer["id"]
        col.update_one({"client_id": payload.client_id}, {"$set": {"stripe_customer_id": customer_id, "email": email, "updated_at": now}})

    # Determine price
    price = payload.price_id
    if not price:
        if payload.interval == "yearly" and STRIPE_PRICE_YEARLY:
            price = STRIPE_PRICE_YEARLY
        else:
            price = STRIPE_PRICE_MONTHLY or STRIPE_PRICE_YEARLY
    if not price:
        raise HTTPException(status_code=500, detail="No Stripe price configured")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price, "quantity": 1}],
            success_url=STRIPE_SUCCESS_URL + "&session_id={CHECKOUT_SESSION_ID}",
            cancel_url=STRIPE_CANCEL_URL,
            allow_promotion_codes=True,
            client_reference_id=payload.client_id,
            metadata={"client_id": payload.client_id},
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@app.post("/api/billing/portal")
def create_billing_portal(payload: BillingPortalPayload):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    col = db[account_collection_name()]
    acct = col.find_one({"client_id": payload.client_id})
    if not acct or not acct.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        portal = stripe.billing_portal.Session.create(
            customer=acct["stripe_customer_id"],
            return_url=STRIPE_SUCCESS_URL,
        )
        return {"portal_url": portal.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        # For safety, reject if not configured
        raise HTTPException(status_code=500, detail="Stripe webhook not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    type_ = event.get("type")
    data = event.get("data", {}).get("object", {})

    def set_plan(client_id: str, plan: str, fields: Dict[str, Any]):
        if db is None:
            return
        now = datetime.now(timezone.utc)
        update = {"plan": plan, "updated_at": now}
        update.update(fields)
        db[account_collection_name()].update_one({"client_id": client_id}, {"$set": update}, upsert=True)

    try:
        if type_ == "checkout.session.completed":
            client_id = data.get("client_reference_id") or (data.get("metadata") or {}).get("client_id")
            sub_id = data.get("subscription")
            cust_id = data.get("customer")
            status = "active"
            if client_id:
                set_plan(client_id, "pro", {"stripe_subscription_id": sub_id, "stripe_customer_id": cust_id, "subscription_status": status})
        elif type_ in ("customer.subscription.created", "customer.subscription.updated"):
            sub_id = data.get("id")
            cust_id = data.get("customer")
            status = data.get("status")
            # Find account by subscription or customer
            if db is not None:
                col = db[account_collection_name()]
                acct = col.find_one({"$or": [
                    {"stripe_subscription_id": sub_id},
                    {"stripe_customer_id": cust_id}
                ]})
                if acct:
                    set_plan(acct["client_id"], "pro" if status in ("active", "trialing") else "free",
                             {"stripe_subscription_id": sub_id, "stripe_customer_id": cust_id, "subscription_status": status})
        elif type_ == "customer.subscription.deleted":
            sub_id = data.get("id")
            status = data.get("status")
            if db is not None:
                col = db[account_collection_name()]
                acct = col.find_one({"stripe_subscription_id": sub_id})
                if acct:
                    set_plan(acct["client_id"], "free", {"subscription_status": status})
    except Exception:
        # Swallow errors to avoid retries storm in dev
        pass

    return {"received": True}


# ---- Session Endpoints ----
@app.post("/api/session")
def start_session(payload: StartSessionPayload):
    category = payload.category
    questions = QUESTION_BANK[category]

    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Ensure account exists
    acc_col = db[account_collection_name()]
    account = acc_col.find_one({"client_id": payload.client_id})
    if not account:
        acc = Account(client_id=payload.client_id, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        create_document(account_collection_name(), acc)
        account = acc_col.find_one({"client_id": payload.client_id})

    plan: Plan = account.get("plan", "free")

    # Enforce free plan limit: 1 session per day (UTC)
    if plan == "free":
        start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = db[session_collection_name()].count_documents({
            "client_id": payload.client_id,
            "created_at": {"$gte": start_of_day}
        })
        if today_count >= 1:
            raise HTTPException(status_code=402, detail="Daily limit reached. Upgrade to Pro for unlimited brainstorming.")

    # Create a new session document
    session_doc = Session(
        category=category,
        name=payload.name,
        goal=payload.goal,
        step=0,
        client_id=payload.client_id,
        plan=plan,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session_id = create_document(session_collection_name(), session_doc)

    return {
        "session_id": session_id,
        "question": questions[0],
        "step": 0,
        "total_steps": len(questions),
        "plan": plan,
    }


@app.get("/api/session/{session_id}/next-question")
def next_question(session_id: str):
    # Fetch session
    sessions = get_documents(session_collection_name(), {"_id": {"$exists": True}})
    session = next((s for s in sessions if str(s.get("_id")) == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    category: Category = session.get("category")
    step: int = session.get("step", 0)
    questions = QUESTION_BANK[category]

    if step >= len(questions):
        return {"done": True}

    return {
        "question": questions[step],
        "step": step,
        "total_steps": len(questions),
    }


@app.post("/api/session/{session_id}/answer")
def submit_answer(session_id: str, payload: AnswerPayload):
    # Load session
    sessions = get_documents(session_collection_name(), {"_id": {"$exists": True}})
    session = next((s for s in sessions if str(s.get("_id")) == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    category: Category = session.get("category")
    step: int = session.get("step", 0)
    questions = QUESTION_BANK[category]

    # Store message
    msg = Message(
        session_id=session_id,
        step=step,
        question=questions[step] if step < len(questions) else "",
        answer=payload.answer,
        created_at=datetime.now(timezone.utc),
    )
    create_document(message_collection_name(), msg)

    # Increment session step
    db[session_collection_name()].update_one(
        {"_id": session["_id"]},
        {"$set": {"step": step + 1, "updated_at": datetime.now(timezone.utc)}}
    )

    # Return next question
    if step + 1 >= len(questions):
        return {"done": True}

    return {
        "question": questions[step + 1],
        "step": step + 1,
        "total_steps": len(questions),
    }


@app.get("/api/session/{session_id}/suggestions")
def get_suggestions(session_id: str):
    # Load session
    sessions = get_documents(session_collection_name(), {"_id": {"$exists": True}})
    session = next((s for s in sessions if str(s.get("_id")) == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    category: Category = session.get("category")

    # Load answers
    messages = get_documents(message_collection_name(), {"session_id": session_id})
    answers = " ".join([(m.get("answer") or "") for m in messages]).lower()

    # Simple heuristic-based suggestion engine
    ideas: List[Dict[str, Any]] = []

    def add_idea(title: str, summary: str, steps: List[str], tags: List[str]):
        doc = Idea(
            session_id=session_id,
            title=title,
            summary=summary,
            steps=steps,
            tags=tags,
            category=category,
            created_at=datetime.now(timezone.utc),
        )
        idea_id = create_document(idea_collection_name(), doc)
        ideas.append({"id": idea_id, "title": title, "summary": summary, "steps": steps, "tags": tags})

    if category == "business":
        if any(k in answers for k in ["ai", "machine learning", "chatbot", "agent"]):
            add_idea(
                "Niche AI Copilot",
                "A focused AI assistant that automates a painful workflow for a specific role.",
                [
                    "Interview 5 target users to validate workflows",
                    "Map the top 3 tasks to automate",
                    "Build a narrow MVP integrating with one tool (e.g., Google Docs)",
                    "Launch a waitlist and onboard 5 pilots",
                ],
                ["ai", "b2b", "automation"],
            )
        if any(k in answers for k in ["marketplace", "platform", "connect"]):
            add_idea(
                "Curated Micro-Marketplace",
                "A small, high-trust marketplace connecting a niche buyer and seller group.",
                [
                    "Define the niche and curation criteria",
                    "Bootstrap supply with personal outreach",
                    "Ship a basic listing + messaging MVP",
                    "Run a limited beta with manual curation",
                ],
                ["marketplace", "community"],
            )
        # Default idea
        add_idea(
            "Service-to-Product Transition",
            "Start with a hands-on service to learn deeply, then productize repeating parts.",
            [
                "List top 3 outcomes you can deliver in 2 weeks",
                "Sell 1-2 projects to validate demand",
                "Document repeatable steps and templatize",
                "Package into a lightweight productized service",
            ],
            ["services", "lean-startup"],
        )

    elif category == "content":
        if any(k in answers for k in ["video", "youtube", "tiktok", "shorts"]):
            add_idea(
                "30-Day Video Sprint",
                "Publish one short video daily to find your voice and audience quickly.",
                [
                    "Pick one theme and 3 content pillars",
                    "Batch-script 7 shorts and record in one session",
                    "Post daily at the same time for 30 days",
                    "Analyze 3 best performers and double down",
                ],
                ["video", "growth"],
            )
        if any(k in answers for k in ["newsletter", "writing", "blog"]):
            add_idea(
                "Opinionated Weekly Newsletter",
                "A tight weekly email with a strong POV and one actionable takeaway.",
                [
                    "Define a recurring structure (hook, insight, action)",
                    "Collect 20 seed ideas from your experiences",
                    "Schedule 2-hour writing blocks weekly",
                    "Add 1 CTA to grow subscribers organically",
                ],
                ["newsletter", "writing"],
            )
        add_idea(
            "Theme + Pillars System",
            "Clarify your content theme and 3 pillars, then plan 12 pieces across formats.",
            [
                "Write your one-sentence positioning",
                "Choose 3 pillars that ladder up to your theme",
                "Create 12 titles (4 per pillar)",
                "Draft a 4-week publishing calendar",
            ],
            ["strategy", "planning"],
        )

    else:  # general
        add_idea(
            "Obstacle Breakdown",
            "Break the challenge into smaller parts and pick one 30-minute task.",
            [
                "List the 3 biggest blockers",
                "Brainstorm 2 ways around each",
                "Pick the easiest next step",
                "Schedule it on your calendar",
            ],
            ["problem-solving", "productivity"],
        )
        if any(k in answers for k in ["time", "schedule", "focus"]):
            add_idea(
                "Time-Boxed Progress",
                "Use fixed time boxes to create momentum and reduce overwhelm.",
                [
                    "Define a clear 7-day outcome",
                    "Set a daily 25-minute focus block",
                    "Track progress with a visible checklist",
                    "Celebrate completion and reflect",
                ],
                ["habits", "execution"],
            )

    return {"suggestions": ideas}
