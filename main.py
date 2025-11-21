import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime, timezone

from database import create_document, get_documents, db
from schemas import Session, Message, Idea

app = FastAPI(title="Thinking Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


class StartSessionPayload(BaseModel):
    category: Category = Field(..., description="Type of brainstorming")
    name: Optional[str] = Field(None, description="Optional user name or alias")
    goal: Optional[str] = Field(None, description="One-line goal or theme for this session")


class AnswerPayload(BaseModel):
    answer: str = Field(..., min_length=1)


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


def session_collection_name() -> str:
    return Session.__name__.lower()


def message_collection_name() -> str:
    return Message.__name__.lower()


def idea_collection_name() -> str:
    return Idea.__name__.lower()


@app.post("/api/session")
def start_session(payload: StartSessionPayload):
    category = payload.category
    questions = QUESTION_BANK[category]

    # Create a new session document
    session_doc = Session(
        category=category,
        name=payload.name,
        goal=payload.goal,
        step=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session_id = create_document(session_collection_name(), session_doc)

    return {
        "session_id": session_id,
        "question": questions[0],
        "step": 0,
        "total_steps": len(questions),
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
