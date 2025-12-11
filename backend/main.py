# main.py - Use this EXACT file
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from supabase import create_client, Client
import stripe
import os
from datetime import datetime

# ===== ENVIRONMENT CONFIGURATION =====
# Secrets are loaded from environment variables (NEVER hardcoded)
# For local development: create a backend/.env file
# For production: set vars in Vercel dashboard

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://your-project.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "your-service-key-here")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "FALLBACK_FOR_LOCAL_DEV_ONLY")
CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET", "FALLBACK_FOR_LOCAL_DEV_ONLY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "FALLBACK_FOR_LOCAL_DEV_ONLY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "FALLBACK_FOR_LOCAL_DEV_ONLY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Initialize clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI()

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

# ===== CLERK AUTHENTICATION (Using YOUR JWKS) =====
import jwt
from jwt import PyJWKClient

JWKS_URL = "https://adequate-lioness-36.clerk.accounts.dev/.well-known/jwks.json"
jwks_client = PyJWKClient(JWKS_URL)

async def get_clerk_user(authorization: Optional[str] = Header(None)):
    """Verify Clerk JWT using YOUR JWKS."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    try:
        token = authorization.replace("Bearer ", "")
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        # Decode without verification first to get kid
        unverified = jwt.decode(token, options={"verify_signature": False})
        
        # Verify signature
        data = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=None,  # Clerk sets audience automatically
            options={"verify_exp": True}
        )
        
        # Return Clerk user ID
        return data.get("sub")
    except Exception as e:
        print(f"Auth error: {e}")
        return None

# ===== CREDIT MANAGEMENT =====
async def get_user_credits(user_id: str) -> int:
    response = supabase.table("users").select("credits_remaining").eq("user_id", user_id).execute()
    if response.data:
        return response.data[0]["credits_remaining"]
    return 0

async def deduct_credit(user_id: str) -> bool:
    credits = await get_user_credits(user_id)
    if credits <= 0:
        return False
    
    supabase.table("users").update({
        "credits_remaining": credits - 1
    }).eq("user_id", user_id).execute()
    return True

# ===== YOUR SCORING ALGORITHM (UNCHANGED) =====
def calculate_vector_score(job_text: str, trauma: TraumaInput) -> dict:
    """Your trauma-informed scoring logic."""
    job = job_text.lower()
    red_flags = []
    green_flags = []
    
    weights = {
        "Safety Baseline": 0.30,
        "ADHD Wiring": 0.20,
        "Capability Fit": 0.25,
        "Co-Regulation": 0.15,
        "Financial Security": 0.10
    }
    
    # ===== DIMENSION 1: SAFETY =====
    safety_base = 50.0
    if "remote-first" in job:
        safety_base += 30 * (11 - trauma.safety_baseline) / 10
        green_flags.append("Remote-first")
    if "on-site" in job or "relocation" in job:
        safety_base -= 40 * (11 - trauma.safety_baseline) / 10
        red_flags.append("On-site requirement")
    
    # ===== DIMENSION 2: ADHD =====
    adhd_base = 50.0
    if "never touch post-launch" in job:
        adhd_base += 50 * (11 - trauma.adhd_wiring) / 10
        green_flags.append("Explicit handoff")
    if "execution" in job and "strategy" not in job:
        adhd_base -= 25 * (11 - trauma.adhd_wiring) / 10
        red_flags.append("Execution focus")
    
    # ===== DIMENSION 3: CAPABILITY =====
    capability_base = 50.0
    if "strategy" in job or "automation" in job:
        capability_base += 35
        green_flags.append("Strategy/automation")
    
    # ===== DIMENSION 4: CO-REGULATION =====
    coreg_base = 50.0
    if "collaborative" in job:
        coreg_base += 20 * (11 - trauma.co_regulation) / 10
        green_flags.append("Collaborative team")
    
    # ===== DIMENSION 5: FINANCIAL =====
    financial_base = 50.0
    if "equity" in job:
        financial_base += 30 * (11 - trauma.financial) / 10
        green_flags.append("Equity compensation")
    
    dimensions = {
        "Safety Baseline": max(0, min(100, safety_base)),
        "ADHD Wiring": max(0, min(100, adhd_base)),
        "Capability Fit": max(0, min(100, capability_base)),
        "Co-Regulation": max(0, min(100, coreg_base)),
        "Financial Security": max(0, min(100, financial_base))
    }
    
    total_score = sum(dimensions[dim] * weights[dim] for dim in dimensions)
    
    # Risk level
    risk = "red" if total_score < 50 else "yellow" if total_score < 75 else "green"
    summary = "Safe for your pattern" if risk == "green" else "Proceed with caution" if risk == "yellow" else "Predicted collapse"
    
    # Vector scores
    vector_scores = []
    critical_gaps = []
    negotiation_priorities = []
    
    for dim_name, dim_score in dimensions.items():
        trauma_dim = dim_name.lower().replace(" ", "_").replace("fit", "").replace("security", "")
        trauma_val = getattr(trauma, trauma_dim, 5)
        match_pct = (dim_score / 100) * 100
        is_critical = dim_score < 50 and trauma_val <= 4
        
        vector_scores.append(VectorScore(
            dimension=dim_name,
            base_score=dim_score,
            trauma_adjusted_score=dim_score * (11 - trauma_val) / 10,
            weight=weights[dim_name],
            match_percentage=round(match_pct, 1),
            risk_level="🔴 Critical" if is_critical else "🟡 Caution" if dim_score < 75 else "🟢 Safe",
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
    authorization: Optional[str] = Header(None)
):
    user_id = await get_clerk_user(authorization)
    is_preview = not user_id or not request.consume_credit
    
    if user_id and request.consume_credit:
        if not await deduct_credit(user_id):
            raise HTTPException(status_code=402, detail="No credits remaining")
    
    result = calculate_vector_score(request.job_description, request.trauma)
    result["preview_mode"] = is_preview
    
    if user_id and request.consume_credit:
        supabase.table("scans").insert({
            "user_id": user_id,
            "job_description": request.job_description,
            "trauma_profile": request.trauma.dict(),
            "vector_result": result,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    
    return JobScanResponse(**result)

@app.post("/api/create-checkout-session")
async def create_checkout_session(
    request: dict,
    authorization: Optional[str] = Header(None)
):
    """Create Stripe Checkout for YOUR $5 product."""
    user_id = await get_clerk_user(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        session = stripe.checkout.Session.create(
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product': 'prod_TaTAljVeSrKAXZ',  # YOUR PRODUCT ID
                    'unit_amount': 500,  # $5.00 in cents
                },
                'quantity': 1,
            }],
            mode='payment',
            client_reference_id=user_id,  # Link to Clerk user
            success_url=f'{FRONTEND_URL}?payment=success',
            cancel_url=f'{FRONTEND_URL}?payment=cancelled',
        )
        return {"sessionId": session.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """Auto-provision 5 credits on sign-up."""
    payload = await request.json()
    
    if payload.get("type") == "user.created":
        user_id = payload["data"]["id"]
        supabase.table("users").insert({
            "user_id": user_id,
            "credits_remaining": 5,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    
    return {"success": True}

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Grant credits after payment."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET")
        )
        
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session["client_reference_id"]
            
            # Add 1 credit per $5
            supabase.rpc("increment_credits", {
                "user_id": user_id,
                "amount": 1
            }).execute()
        
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/user/credits")
async def get_credits(authorization: Optional[str] = Header(None)):
    """Get user credit balance."""
    user_id = await get_clerk_user(authorization)
    if not user_id:
        return {"credits": 0, "user_id": None}
    
    response = supabase.table("users").select("credits_remaining").eq("user_id", user_id).execute()
    if response.data:
        return {"credits": response.data[0]["credits_remaining"], "user_id": user_id}
    return {"credits": 0, "user_id": user_id}

@app.get("/")
async def serve_frontend():
    with open("index.html", "r") as f:
        return f.read()