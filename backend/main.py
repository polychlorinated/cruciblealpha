# ===== IMPORTS =====
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
from supabase import create_client, Client
from pathlib import Path
from dotenv import load_dotenv
import stripe
import jwt
from jwt import PyJWKClient
import os
from datetime import datetime

# ===== ENVIRONMENT CONFIGURATION =====
load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY")
CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY = os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")

# Validate critical env vars
required_secrets = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "CLERK_SECRET_KEY"]
for var in required_secrets:
    if not os.getenv(var):
        print(f"⚠️ WARNING: {var} not set!")

# Initialize clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI()

# ===== CORS FOR CODESPACES =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://fuzzy-space-dollop-gg555q4wgr5hpvr6-8000.app.github.dev",
        "http://localhost:8000",
        "http://127.0.0.1:8000"
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
    module_id: Optional[str] = None  # For updating existing module

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

# ===== CLERK AUTHENTICATION =====
JWKS_URL = "https://adequate-lioness-36.clerk.accounts.dev/.well-known/jwks.json"
jwks_client = PyJWKClient(JWKS_URL)

async def get_clerk_user(authorization: Optional[str] = Header(None)):
    """Verify Clerk JWT using YOUR JWKS."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    try:
        token = authorization.replace("Bearer ", "")
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

async def add_credits(user_id: str, amount: int):
    supabase.table("users").update({
        "credits_remaining": supabase.raw(f"credits_remaining + {amount}")
    }).eq("user_id", user_id).execute()

# ===== VECTOR SCORING ALGORITHM =====
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
    
    # Build vector scores
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
    """Scan job with credit deduction and module management."""
    user_id = await get_clerk_user(authorization)
    is_preview = not user_id or not request.consume_credit
    
    # Deduct credit if authenticated
    if user_id and request.consume_credit:
        if not await deduct_credit(user_id):
            raise HTTPException(status_code=402, detail="No credits remaining")
    
    # Calculate results
    result = calculate_vector_score(request.job_description, request.trauma)
    result["preview_mode"] = is_preview
    
    # Save to module
    if user_id:
        module_data = {
            "user_id": user_id,
            "survey_data": request.trauma.dict(),
            "job_description": request.job_description,
            "scan_results": result,
            "completed_at": datetime.utcnow().isoformat(),
            "is_active": True
        }
        
        # Deactivate old active module
        supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()
        
        # Insert new module
        supabase.table("modules").insert(module_data).execute()
    
    return JobScanResponse(**result)

@app.post("/api/create-checkout-session")
async def create_checkout_session(
    request: StripeCheckoutRequest,
    authorization: Optional[str] = Header(None)
):
    """Create Stripe Checkout for credit purchases."""
    user_id = await get_clerk_user(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Get or create Stripe customer
    user_response = supabase.table("users").select("stripe_customer_id").eq("user_id", user_id).execute()
    customer_id = None
    if user_response.data and user_response.data[0].get("stripe_customer_id"):
        customer_id = user_response.data[0]["stripe_customer_id"]
    else:
        # Create new customer
        customer = stripe.Customer.create(
            email=supabase.table("users").select("email").eq("user_id", user_id).execute().data[0]["email"],
            metadata={"clerk_user_id": user_id}
        )
        customer_id = customer.id
        supabase.table("users").update({"stripe_customer_id": customer_id}).eq("user_id", user_id).execute()
    
    try:
        session = stripe.checkout.Session.create(
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': 500,  # $5.00 per credit
                    'product_data': {
                        'name': f'{request.credits_to_purchase} Credit(s)',
                        'description': 'AOJA Job Scan Credits'
                    },
                },
                'quantity': request.credits_to_purchase,
            }],
            mode='payment',
            client_reference_id=user_id,
            customer=customer_id,
            success_url=f'{FRONTEND_URL}?payment=success',
            cancel_url=f'{FRONTEND_URL}?payment=cancelled',
        )
        return {"sessionId": session.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

@app.get("/api/user/modules")
async def get_modules(authorization: Optional[str] = Header(None)):
    """Get all modules for user."""
    user_id = await get_clerk_user(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    response = supabase.table("modules").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return {"modules": response.data or []}

@app.post("/api/user/retake-survey")
async def retake_survey(authorization: Optional[str] = Header(None)):
    """Start new module (retake survey)."""
    user_id = await get_clerk_user(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Deactivate current active module
    supabase.table("modules").update({"is_active": False}).eq("user_id", user_id).eq("is_active", True).execute()
    
    return {"success": True}

@app.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """Auto-provision 5 credits and create user record on sign-up."""
    payload = await request.json()
    
    if payload.get("type") == "user.created":
        user_id = payload["data"]["id"]
        
        # Get email from payload (safely)
        email_addresses = payload["data"].get("email_addresses", [])
        primary_email = "user@unknown.com"  # Fallback - will be updated by Clerk sync
        
        if email_addresses and len(email_addresses) > 0:
            primary_email = email_addresses[0].get("email_address", primary_email)
        
        # Create user record with 5 credits
        supabase.table("users").insert({
            "user_id": user_id,
            "credits_remaining": 5,
            "email": primary_email,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        
        print(f"✅ Created user {user_id} ({primary_email}) with 5 credits")
    
    return {"success": True}

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Grant credits after payment and handle module transfers."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session["client_reference_id"]
            credits_purchased = session["line_items"]["data"][0]["quantity"]
            
            # Add credits
            await add_credits(user_id, credits_purchased)
            
            # Check for pending localStorage module transfer
            # This will be handled in frontend on payment success
        
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve frontend with dynamic environment injection."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"
    
    with open(frontend_path, "r") as f:
        html_content = f.read()
    
    # Inject Clerk publishable key
    clerk_key = NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY or 'your_fallback_key'
    html_content = html_content.replace(
        'data-clerk-publishable-key=""',
        f'data-clerk-publishable-key="{clerk_key}"'
    )
    
    # Inject Stripe publishable key for JavaScript
    stripe_key = os.getenv("NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY", "")
    html_content = html_content.replace(
        "window.STRIPE_PUBLISHABLE_KEY = '%%STRIPE_PUBLISHABLE_KEY%%';",
        f"window.STRIPE_PUBLISHABLE_KEY = '{stripe_key}';"
    )
    
    return HTMLResponse(content=html_content)

@app.get("/{full_path:path}")
async def serve_frontend_catch_all(full_path: str):
    """Catch-all for SPA routing."""
    if full_path.startswith("api/") or full_path.startswith("webhooks/"):
        return None
    
    return await serve_frontend()