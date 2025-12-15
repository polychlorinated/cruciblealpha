# ===== IMPORTS =====
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

# ===== ENVIRONMENT CONFIGURATION =====
load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY")  # not used directly; JWT validated via JWKS
CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY = os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY = os.getenv("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY", "")

# Validate critical env vars
required_secrets = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "CLERK_SECRET_KEY"]
for var in required_secrets:
    if not os.getenv(var):
        print(f"WARNING: {var} not set!")

# Initialize clients
supabase: Client
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    supabase = None  # will be replaced by fake supabase if configured

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI()

# Development-only: simple in-memory fake supabase for local testing when configured.
if os.getenv("USE_FAKE_SUPABASE", "0") == "1":
    class _InMemoryTable:
        def __init__(self):
            self._data = []

        def update(self, obj):
            self._update_obj = obj
            return self

        def insert(self, obj):
            # naive insert: assign id if missing
            if isinstance(obj, dict) and "id" not in obj:
                obj = {**obj, "id": f"fake_{len(self._data)+1}"}
            self._data.insert(0, obj)

            class Exec:
                def __init__(self, data):
                    self.data = [data]
                def execute(self):
                    return self
            return Exec(obj)

        def select(self, *args, **kwargs):
            outer = self
            class Exec:
                def __init__(self):
                    self.data = outer._data
                    self._filters = []
                def eq(self, key, val):
                    self._filters.append((key, val))
                    self.data = [r for r in self.data if isinstance(r, dict) and r.get(key) == val]
                    return self
                def order(self, *a, **k):
                    return self
                def limit(self, n):
                    self.data = self.data[:n]
                    return self
                def execute(self):
                    return self
            return Exec()

        def eq(self, key, val):
            self._eq = (key, val)
            return self

        def execute(self):
            # naive update apply for eq
            if hasattr(self, "_update_obj") and hasattr(self, "_eq"):
                k, v = self._eq
                for i, row in enumerate(self._data):
                    if isinstance(row, dict) and row.get(k) == v:
                        self._data[i] = {**row, **self._update_obj}
            class R:
                data = []
            return R()

    class _FakeSupabase:
        def __init__(self):
            self._tables = {}
        def table(self, name):
            if name not in self._tables:
                self._tables[name] = _InMemoryTable()
            return self._tables[name]

    print("Using in-memory fake Supabase (USE_FAKE_SUPABASE=1)")
    supabase = _FakeSupabase()

if supabase is None:
    print("ERROR: Supabase is not configured and USE_FAKE_SUPABASE is not enabled.")

# ===== CORS =====
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

# ===== DATA MODELS =====
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
    module_id: Optional[str] = None

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

# ===== CLERK AUTHENTICATION =====
JWKS_URL = "https://adequate-lioness-36.clerk.accounts.dev/.well-known/jwks.json"
jwks_client = PyJWKClient(JWKS_URL)
CLERK_DEV_BYPASS = os.getenv("CLERK_DEV_BYPASS", "0") == "1"

async def get_clerk_user(authorization: Optional[str] = Header(None), request: Optional[Request] = None):
    """
    Verify Clerk JWT using JWKS. Returns user_id (sub) or None.

    Dev bypass: if CLERK_DEV_BYPASS=1, allow X-DEV-USER header.
    """
    if CLERK_DEV_BYPASS and request is not None:
        x_dev_user = request.headers.get("x-dev-user")
        if x_dev_user:
            return x_dev_user

    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization.replace("Bearer ", "")
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        data = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_exp": True}
        )
        return data.get("sub")
    except Exception as e:
        print(f"Auth error: {e}")
        return None

@app.get("/api/debug/whoami")
async def debug_whoami(authorization: Optional[str] = Header(None), rq: Request = None):
    user_id = await get_clerk_user(authorization, rq)
    return {"user_id": user_id}

# ===== CREDIT MANAGEMENT =====
async def get_user_credits(user_id: str) -> int:
    if supabase is None:
        return 0
    response = supabase.table("users").select("credits_remaining").eq("user_id", user_id).execute()
    if response.data:
        return int(response.data[0].get("credits_remaining", 0))
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

# ===== VECTOR SCORING ALGORITHM =====
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

    # Safety dimension
    safety_base = 50.0
    if "remote-first" in job:
        safety_base += 30 * (11 - trauma.safety_baseline) / 10
        green_flags.append("Remote-first")
    if "on-site" in job or "relocation" in job:
        safety_base -= 40 * (11 - trauma.safety_baseline) / 10
        red_flags.append("On-site requirement")

    # ADHD dimension
    adhd_base = 50.0
    if "deep work" in job or "focus time" in job:
        adhd_base += 40 * (11 - trauma.adhd_wiring) / 10
        green_flags.append("Deep work protected")
    if "fast-paced" in job or "multitasking" in job:
        adhd_base -= 30 * (11 - trauma.adhd_wiring) / 10
        red_flags.append("High context switching")

    # Capability dimension
    capability_base = 50.0
    if "automation" in job or "strategy" in job:
        capability_base += 35
        green_flags.append("Strategic/automation focus")

    # Co-regulation dimension
    coreg_base = 50.0
    if "collaborative" in job or "team-oriented" in job:
        coreg_base += 20 * (11 - trauma.co_regulation) / 10
        green_flags.append("Collaborative culture")
    if "independent" in job and "team" not in job:
        coreg_base -= 15 * (11 - trauma.co_regulation) / 10
        red_flags.append("Potentially isolated")

    # Financial dimension
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

    total_score = sum(dimensions[dim] * weights[dim] for dim in dimensions)
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
        match_pct = (dim_score / 100) * 100
        is_critical = dim_score < 50 and trauma_val <= 4

        vector_scores.append(VectorScore(
            dimension=dim_name,
            base_score=dim_score,
            trauma_adjusted_score=dim_score * (11 - trauma_val) / 10,
            weight=weights[dim_name],
            match_percentage=round(match_pct, 1),
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

# ===== API ENDPOINTS =====

@app.post("/api/scan-job", response_model=JobScanResponse)
async def scan_job_endpoint(
    request: JobScanRequest,
    authorization: Optional[str] = Header(None),
    rq: Request = None
):
    """
    Scan job with credit deduction.
    Credits are charged per scan only (consume_credit=True).
    """
    user_id = await get_clerk_user(authorization, rq)
    is_preview = not user_id or not request.consume_credit

    # Deduct credit if authenticated and this is a real scan
    if user_id and request.consume_credit:
        if not await deduct_credit(user_id):
            raise HTTPException(status_code=402, detail="No credits remaining")

    # Calculate results
    result = calculate_vector_score(request.job_description, request.trauma)
    result["preview_mode"] = is_preview

    # Persist module for authenticated real scans only
    if supabase is not None and user_id and request.consume_credit:
        # Deactivate old active module
        supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()

        # Convert VectorScore objects to dicts for JSON serialization
        dimensional_match = []
        for dim in result.get("dimensional_match", []):
            if hasattr(dim, "dict"):
                dimensional_match.append(dim.dict())
            else:
                dimensional_match.append(dict(dim))

        module_data = {
            "user_id": user_id,
            "job_description": (request.job_description or "")[:2000],
            "survey_data": request.trauma.dict(),
            "scan_results": {
                "risk_level": result.get("risk_level"),
                "summary": result.get("summary"),
                "overall_score": result.get("overall_score"),
                "dimensional_match": dimensional_match,
                "critical_gaps": result.get("critical_gaps", []),
                "negotiation_priorities": result.get("negotiation_priorities", []),
                "green_flags": result.get("green_flags", []),
                "red_flags": result.get("red_flags", []),
            },
            "created_at": datetime.utcnow().isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "is_active": True
        }
        supabase.table("modules").insert(module_data).execute()

    return JobScanResponse(**result)

@app.post("/api/user/transfer-pending-module")
async def transfer_pending_module(
    body: TransferModuleRequest,
    authorization: Optional[str] = Header(None),
    rq: Request = None
):
    """
    Persist a survey-only module (or transferred guest module) to the authenticated user.
    This does not consume credits.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Deactivate old active module
    supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()

    supabase.table("modules").insert({
        "user_id": user_id,
        "survey_data": body.survey,
        "job_description": body.job_description or "",
        "scan_results": body.scan_results,  # may be null
        "is_active": True,
        "created_at": body.created_at,
        "completed_at": datetime.utcnow().isoformat() if body.scan_results else None
    }).execute()

    return {"success": True}

@app.post("/api/user/initialize")
async def initialize_user(authorization: Optional[str] = Header(None), rq: Request = None):
    """Ensure user exists in supabase and has initial credits (5)."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    resp = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if not resp.data:
        supabase.table("users").insert({
            "user_id": user_id,
            "credits_remaining": 5,
            "email": f"user_{user_id[:8]}@clerk.dev",
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        return {"credits": 5}

    credits = resp.data[0].get("credits_remaining", 0)
    return {"credits": credits}

@app.get("/api/user/profile")
async def get_user_profile(authorization: Optional[str] = Header(None), rq: Request = None):
    """
    Return the latest module as a 'profile' object that includes survey + scan_results (if any).
    This allows the frontend to always have profile.survey for scanning.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    resp = supabase.table("modules") \
        .select("survey_data, scan_results, created_at, completed_at") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if resp.data and len(resp.data) > 0:
        row = resp.data[0]
        scan = row.get("scan_results") or {}
        profile = {
            "survey": row.get("survey_data") or {},
            **scan
        }
        return {"profile": profile}

    return {"profile": None}

@app.get("/api/user/credits")
async def get_credits(authorization: Optional[str] = Header(None), rq: Request = None):
    """Get user credit balance."""
    if supabase is None:
        return {"credits": 0, "user_id": None}

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        return {"credits": 0, "user_id": None}

    response = supabase.table("users").select("credits_remaining").eq("user_id", user_id).execute()
    if response.data:
        return {"credits": response.data[0]["credits_remaining"], "user_id": user_id}
    return {"credits": 0, "user_id": user_id}

@app.get("/api/user/modules")
async def get_modules(authorization: Optional[str] = Header(None), rq: Request = None):
    """Get all modules for user."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    response = supabase.table("modules").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"modules": response.data or []}

@app.post("/api/user/retake-survey")
async def retake_survey(authorization: Optional[str] = Header(None), rq: Request = None):
    """Deactivate current active module so user can start fresh survey."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()
    return {"success": True}

@app.post("/api/create-checkout-session")
async def create_checkout_session(
    request: StripeCheckoutRequest,
    authorization: Optional[str] = Header(None),
    rq: Request = None
):
    """Create Stripe Checkout session to buy credits."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    user_id = await get_clerk_user(authorization, rq)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        user_response = supabase.table("users").select("*").eq("user_id", user_id).execute()

        if not user_response.data:
            # Create user if not present yet
            supabase.table("users").insert({
                "user_id": user_id,
                "credits_remaining": 5,
                "email": f"user_{user_id[:8]}@clerk.dev",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            user_data = {"user_id": user_id, "email": f"user_{user_id[:8]}@clerk.dev"}
        else:
            user_data = user_response.data[0]

        customer_id = user_data.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(
                email=user_data.get("email", f"user_{user_id[:8]}@clerk.dev"),
                metadata={"clerk_user_id": user_id}
            )
            customer_id = customer.id
            supabase.table("users").update({"stripe_customer_id": customer_id}).eq("user_id", user_id).execute()

        session = stripe.checkout.Session.create(
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 500,  # $5.00 per credit
                    "product_data": {
                        "name": f"{request.credits_to_purchase} Credit(s)",
                        "description": "AOJA Job Scan Credits - One-time purchase"
                    },
                },
                "quantity": request.credits_to_purchase,
            }],
            mode="payment",
            client_reference_id=user_id,
            customer=customer_id,
            metadata={"credits_to_purchase": str(request.credits_to_purchase)},
            success_url=f"{FRONTEND_URL}?payment=success&credits={request.credits_to_purchase}",
            cancel_url=f"{FRONTEND_URL}?payment=cancelled",
        )

        return {"sessionId": session.id}
    except Exception as e:
        print(f"Stripe error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """Auto-provision 5 credits and create user record on sign-up."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    payload = await request.json()

    if payload.get("type") == "user.created":
        user_id = payload["data"]["id"]

        email_addresses = payload["data"].get("email_addresses", [])
        primary_email = "user@unknown.com"
        if email_addresses and len(email_addresses) > 0:
            primary_email = email_addresses[0].get("email_address", primary_email)

        # Create user record with 5 credits (best-effort; ignore if already exists)
        existing = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
        if not existing.data:
            supabase.table("users").insert({
                "user_id": user_id,
                "credits_remaining": 5,
                "email": primary_email,
                "created_at": datetime.utcnow().isoformat()
            }).execute()

    return {"success": True}

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Grant credits after Stripe payment completes."""
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session.get("client_reference_id")
            credits_purchased = int(session.get("metadata", {}).get("credits_to_purchase", "1"))

            if user_id:
                await add_credits(user_id, credits_purchased)

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve frontend with dynamic environment injection."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"

    with open(frontend_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Inject Clerk key into data-clerk-publishable-key="..."
    clerk_key = NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY or "pk_test_fallback"
    html_content = re.sub(
        r'data-clerk-publishable-key="[^"]*"',
        f'data-clerk-publishable-key="{clerk_key}"',
        html_content,
        count=1
    )

    # Inject Stripe key (matches the INJECT_ME placeholder)
    html_content = html_content.replace(
        "window.STRIPE_PUBLISHABLE_KEY = 'INJECT_ME';",
        f"window.STRIPE_PUBLISHABLE_KEY = '{NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY}';"
    )

    return HTMLResponse(content=html_content)

@app.get("/{full_path:path}")
async def serve_frontend_catch_all(full_path: str):
    """Catch-all for SPA routing."""
    if full_path.startswith("api/") or full_path.startswith("webhooks/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return await serve_frontend()