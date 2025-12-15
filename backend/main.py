from __future__ import annotations

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from supabase import create_client, Client
from pathlib import Path
from dotenv import load_dotenv
import stripe
import jwt
from jwt import PyJWKClient
import os
from datetime import datetime
import re


# =========================================================
# ENV LOADING (robust)
# =========================================================
BASE_DIR = Path(__file__).resolve().parent.parent
CANDIDATE_ENVS = [
    BASE_DIR / ".env",
    BASE_DIR / "backend" / ".env",
    Path.cwd() / ".env",
    Path.cwd() / "backend" / ".env",
]

loaded_any = False
for p in CANDIDATE_ENVS:
    if p.exists():
        load_dotenv(p, override=True)
        print(f"Loaded .env from: {p}")
        loaded_any = True

if not loaded_any:
    print("WARNING: No .env file found. Tried:", [str(p) for p in CANDIDATE_ENVS])


# =========================================================
# READ ENV VARS
# =========================================================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()

STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

FRONTEND_URL = (os.getenv("FRONTEND_URL") or "http://localhost:8000").strip()

NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY = (os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY") or "").strip()
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY = (os.getenv("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY") or "").strip()

CLERK_DEV_BYPASS = (os.getenv("CLERK_DEV_BYPASS") or "0") == "1"

JWKS_URL = "https://adequate-lioness-36.clerk.accounts.dev/.well-known/jwks.json"


def _warn_if_not_service_role():
    if not SUPABASE_SERVICE_KEY:
        print("WARNING: SUPABASE_SERVICE_KEY is missing")
        return
    try:
        payload = jwt.decode(SUPABASE_SERVICE_KEY, options={"verify_signature": False})
        role = payload.get("role")
        if role != "service_role":
            print(f"WARNING: SUPABASE_SERVICE_KEY role is '{role}', expected 'service_role'. RLS will block writes.")
    except Exception as e:
        print("WARNING: Could not decode SUPABASE_SERVICE_KEY:", e)


_warn_if_not_service_role()


# =========================================================
# INIT CLIENTS
# =========================================================
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("WARNING: SUPABASE_URL or SUPABASE_SERVICE_KEY missing. Supabase features will fail.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY missing. Stripe checkout will fail.")


# =========================================================
# FASTAPI APP
# =========================================================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# MODELS
# =========================================================
class TraumaInput(BaseModel):
    safety_baseline: int = Field(..., ge=1, le=10)
    adhd_wiring: int = Field(..., ge=1, le=10)
    capability: int = Field(..., ge=1, le=10)
    co_regulation: int = Field(..., ge=1, le=10)
    financial: int = Field(..., ge=1, le=10)

class JobScanRequest(BaseModel):
    job_description: str
    trauma: TraumaInput
    user_id: Optional[str] = None
    consume_credit: bool = True

class VectorScore(BaseModel):
    dimension: str
    base_score: float
    trauma_adjusted_score: float
    weight: float
    match_percentage: float
    risk_level: str
    critical: bool

class JobScanResponse(BaseModel):
    overall_score: float
    risk_level: str
    summary: str
    dimensional_match: List[VectorScore]
    critical_gaps: List[str]
    negotiation_priorities: List[str]
    red_flags: List[str]
    green_flags: List[str]
    credit_cost: int = 1
    preview_mode: bool = False

class StripeCheckoutRequest(BaseModel):
    credits_to_purchase: int = Field(..., ge=1, le=100)

class TransferModuleRequest(BaseModel):
    survey: Dict[str, Any]
    job_description: Optional[str] = None
    scan_results: Optional[Dict[str, Any]] = None
    created_at: str


# =========================================================
# CLERK AUTH
# =========================================================
jwks_client = PyJWKClient(JWKS_URL)

async def get_clerk_user(authorization: Optional[str], rq: Optional[Request]) -> Optional[str]:
    if CLERK_DEV_BYPASS and rq is not None:
        dev_user = rq.headers.get("x-dev-user")
        if dev_user:
            return dev_user

    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization.replace("Bearer ", "")
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        return payload.get("sub")
    except Exception as e:
        print("Auth error:", str(e))
        return None


# =========================================================
# DEBUG
# =========================================================
@app.get("/api/debug/env")
async def debug_env():
    k = NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
    return {
        "has_clerk_publishable_key": bool(k),
        "clerk_key_prefix": k[:10],
        "clerk_key_len": len(k),
        "has_supabase": bool(SUPABASE_URL and SUPABASE_SERVICE_KEY),
        "frontend_url": FRONTEND_URL,
    }


# =========================================================
# USERS / CREDITS (Supabase schema: users.user_id (text), credits_remaining (int))
# =========================================================
async def ensure_user_row(user_id: str):
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    resp = supabase.table("users").select("user_id, credits_remaining").eq("user_id", user_id).execute()
    if resp.data:
        return

    # email is nullable in your live schema
    supabase.table("users").insert({
        "user_id": user_id,
        "credits_remaining": 5,
        "email": None,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

async def get_user_credits(user_id: str) -> int:
    if supabase is None:
        return 0
    resp = supabase.table("users").select("credits_remaining").eq("user_id", user_id).execute()
    if resp.data:
        return int(resp.data[0].get("credits_remaining", 0))
    return 0

async def deduct_credit(user_id: str) -> bool:
    if supabase is None:
        return False
    credits = await get_user_credits(user_id)
    if credits <= 0:
        return False
    supabase.table("users").update({"credits_remaining": credits - 1}).eq("user_id", user_id).execute()
    return True

async def add_credits(user_id: str, amount: int):
    if supabase is None:
        return
    credits = await get_user_credits(user_id)
    supabase.table("users").update({"credits_remaining": credits + amount}).eq("user_id", user_id).execute()


# =========================================================
# SCORING
# =========================================================
def calculate_vector_score(job_text: str, trauma: TraumaInput) -> dict:
    job = (job_text or "").lower()
    red_flags: List[str] = []
    green_flags: List[str] = []

    weights = {
        "Safety Baseline": 0.30,
        "ADHD Wiring": 0.20,
        "Capability Fit": 0.25,
        "Co-Regulation": 0.15,
        "Financial Security": 0.10
    }

    safety_base = 50.0
    if "remote-first" in job:
        safety_base += 30 * (11 - trauma.safety_baseline) / 10
        green_flags.append("Remote-first")
    if "on-site" in job or "relocation" in job:
        safety_base -= 40 * (11 - trauma.safety_baseline) / 10
        red_flags.append("On-site requirement")

    adhd_base = 50.0
    if "deep work" in job or "focus time" in job:
        adhd_base += 40 * (11 - trauma.adhd_wiring) / 10
        green_flags.append("Deep work protected")
    if "fast-paced" in job or "multitasking" in job:
        adhd_base -= 30 * (11 - trauma.adhd_wiring) / 10
        red_flags.append("High context switching")

    capability_base = 50.0
    if "automation" in job or "strategy" in job:
        capability_base += 35
        green_flags.append("Strategic/automation focus")

    coreg_base = 50.0
    if "collaborative" in job or "team-oriented" in job:
        coreg_base += 20 * (11 - trauma.co_regulation) / 10
        green_flags.append("Collaborative culture")
    if "independent" in job and "team" not in job:
        coreg_base -= 15 * (11 - trauma.co_regulation) / 10
        red_flags.append("Potentially isolated")

    financial_base = 50.0
    if "transparent pay" in job or "salary range" in job:
        financial_base += 25 * (11 - trauma.financial) / 10
        green_flags.append("Salary transparency")

    dimensions = {
        "Safety Baseline": max(0, min(100, safety_base)),
        "ADHD Wiring": max(0, min(100, adhd_base)),
        "Capability Fit": max(0, min(100, capability_base)),
        "Co-Regulation": max(0, min(100, coreg_base)),
        "Financial Security": max(0, min(100, financial_base))
    }

    total_score = sum(dimensions[d] * weights[d] for d in dimensions)
    risk = "red" if total_score < 50 else "yellow" if total_score < 75 else "green"
    summary = "Safe for your pattern" if risk == "green" else "Proceed with caution" if risk == "yellow" else "Predicted collapse"

    trauma_field_map = {
        "Safety Baseline": "safety_baseline",
        "ADHD Wiring": "adhd_wiring",
        "Capability Fit": "capability",
        "Co-Regulation": "co_regulation",
        "Financial Security": "financial",
    }

    vector_scores: List[VectorScore] = []
    critical_gaps: List[str] = []
    negotiation_priorities: List[str] = []

    for dim_name, dim_score in dimensions.items():
        trauma_val = getattr(trauma, trauma_field_map[dim_name])
        is_critical = dim_score < 50 and trauma_val <= 4

        vector_scores.append(VectorScore(
            dimension=dim_name,
            base_score=dim_score,
            trauma_adjusted_score=dim_score * (11 - trauma_val) / 10,
            weight=weights[dim_name],
            match_percentage=round(dim_score, 1),
            risk_level="Critical" if is_critical else "Caution" if dim_score < 75 else "Safe",
            critical=is_critical
        ))

        if is_critical:
            critical_gaps.append(f"{dim_name}: {dim_score:.0f}/100")
            if "ADHD" in dim_name:
                negotiation_priorities.append("Negotiate explicit handoff clause")

    return {
        "overall_score": round(total_score, 2),
        "risk_level": risk,
        "summary": summary,
        "dimensional_match": vector_scores,
        "critical_gaps": critical_gaps,
        "negotiation_priorities": negotiation_priorities,
        "red_flags": red_flags,
        "green_flags": green_flags,
        "credit_cost": 1,
        "preview_mode": False
    }


# =========================================================
# API ENDPOINTS
# =========================================================
@app.post("/api/user/initialize")
async def initialize_user(authorization: Optional[str] = Header(None), rq: Request = None):
    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await ensure_user_row(user_id)
    credits = await get_user_credits(user_id)
    return {"credits": credits}


@app.get("/api/user/credits")
async def get_credits(authorization: Optional[str] = Header(None), rq: Request = None):
    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        return {"credits": 0, "user_id": None}
    await ensure_user_row(user_id)
    credits = await get_user_credits(user_id)
    return {"credits": credits, "user_id": user_id}


@app.get("/api/user/profile")
async def get_user_profile(authorization: Optional[str] = Header(None), rq: Request = None):
    """
    Returns latest module profile for the user. Includes survey + scan_results.
    This is what the frontend uses after sign-in to restore state.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await ensure_user_row(user_id)

    resp = (
        supabase.table("modules")
        .select("survey_data, scan_results, created_at, completed_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not resp.data:
        return {"profile": None}

    row = resp.data[0]
    scan = row.get("scan_results") or {}
    profile = {"survey": row.get("survey_data") or {}, **scan}
    return {"profile": profile}


@app.get("/api/user/modules")
async def get_modules(authorization: Optional[str] = Header(None), rq: Request = None):
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    resp = supabase.table("modules").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"modules": resp.data or []}


@app.post("/api/user/retake-survey")
async def retake_survey(authorization: Optional[str] = Header(None), rq: Request = None):
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()
    return {"success": True}


@app.post("/api/user/transfer-pending-module")
async def transfer_pending_module(
    body: TransferModuleRequest,
    authorization: Optional[str] = Header(None),
    rq: Request = None
):
    """
    Used to transfer guest profile/survey into the authenticated user's account.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    await ensure_user_row(user_id)

    supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()

    supabase.table("modules").insert({
        "user_id": user_id,
        "survey_data": body.survey,
        "job_description": body.job_description or "",
        "scan_results": body.scan_results,
        "is_active": True,
        "created_at": body.created_at,
        "completed_at": datetime.utcnow().isoformat() if body.scan_results else None,
        "metadata": {"source": "transfer-pending-module"}
    }).execute()

    return {"success": True}


@app.post("/api/scan-job", response_model=JobScanResponse)
async def scan_job_endpoint(
    request: JobScanRequest,
    authorization: Optional[str] = Header(None),
    rq: Request = None
):
    """
    - If authenticated AND consume_credit=True: deduct 1 credit and save module.
    - Otherwise: preview mode, no credit, no module insertion.
    """
    user_id = await get_clerk_user(authorization, rq)
    is_preview = (not user_id) or (not request.consume_credit)

    if user_id:
        await ensure_user_row(user_id)

    if user_id and request.consume_credit:
        ok = await deduct_credit(user_id)
        if not ok:
            raise HTTPException(status_code=402, detail="No credits remaining")

    result = calculate_vector_score(request.job_description, request.trauma)
    result["preview_mode"] = is_preview

    if supabase is not None and user_id and request.consume_credit:
        supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()
        dimensional_match = [vs.dict() for vs in result["dimensional_match"]]

        supabase.table("modules").insert({
            "user_id": user_id,
            "survey_data": request.trauma.dict(),
            "job_description": request.job_description or "",
            "scan_results": {
                "overall_score": result.get("overall_score"),
                "risk_level": result.get("risk_level"),
                "summary": result.get("summary"),
                "dimensional_match": dimensional_match,
                "critical_gaps": result.get("critical_gaps", []),
                "negotiation_priorities": result.get("negotiation_priorities", []),
                "green_flags": result.get("green_flags", []),
                "red_flags": result.get("red_flags", []),
            },
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "metadata": {"source": "scan-job"}
        }).execute()

    return JobScanResponse(**result)


# =========================================================
# FRONTEND SERVING + KEY INJECTION
# =========================================================
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    frontend_path = BASE_DIR / "frontend" / "index.html"
    if not frontend_path.exists():
        raise HTTPException(status_code=500, detail=f"frontend/index.html not found at {frontend_path}")

    html_content = frontend_path.read_text(encoding="utf-8")

    if not NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:
        raise HTTPException(status_code=500, detail="NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY is missing on server")

    html_content, n = re.subn(
        r'data-clerk-publishable-key="[^"]*"',
        f'data-clerk-publishable-key="{NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY}"',
        html_content,
        count=1
    )
    if n != 1:
        raise HTTPException(status_code=500, detail="Could not inject Clerk key (attribute not found)")

    html_content = html_content.replace(
        "window.STRIPE_PUBLISHABLE_KEY = 'INJECT_ME';",
        f"window.STRIPE_PUBLISHABLE_KEY = '{NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY}';"
    )

    return HTMLResponse(content=html_content)


@app.get("/{full_path:path}")
async def serve_frontend_catch_all(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("webhooks/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return await serve_frontend()