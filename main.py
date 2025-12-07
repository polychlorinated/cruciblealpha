from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator
from typing import Optional
import re

app = FastAPI(
    title="AOJA Job Scanner",
    description="Analyze job descriptions for Autonomy, Openness, Justice, and Authenticity",
    version="1.0.0"
)


class JobScanRequest(BaseModel):
    """Input model for job description scanning."""
    job_description: str = Field(
        ...,
        min_length=50,
        description="Job description text to analyze (minimum 50 characters)"
    )
    company_name: Optional[str] = Field(
        None,
        description="Optional company name for context"
    )

    @validator('job_description')
    def validate_description(cls, v):
        """Ensure job description has meaningful content."""
        if not v or len(v.strip()) < 50:
            raise ValueError('Job description must be at least 50 characters')
        return v.strip()


class AOJABreakdown(BaseModel):
    """Breakdown of individual AOJA components."""
    autonomy: float = Field(..., ge=0, le=100, description="Autonomy score (0-100)")
    openness: float = Field(..., ge=0, le=100, description="Openness score (0-100)")
    justice: float = Field(..., ge=0, le=100, description="Justice score (0-100)")
    authenticity: float = Field(..., ge=0, le=100, description="Authenticity score (0-100)")


class JobScanResponse(BaseModel):
    """Output model for job scan results."""
    aoja_score: float = Field(..., ge=0, le=100, description="Overall AOJA score (0-100)")
    breakdown: AOJABreakdown = Field(..., description="Individual component scores")
    risk_level: str = Field(..., description="Risk assessment: 'red', 'yellow', or 'green'")
    red_flags: list[str] = Field(default_factory=list, description="Identified red flags")
    green_flags: list[str] = Field(default_factory=list, description="Identified positive signals")
    summary: str = Field(..., description="Brief assessment summary")

# DELETE everything from "def analyze_autonomy" to "def calculate_aoja_score"
# REPLACE with this:

class TraumaScannerInput(BaseModel):
    """Input: 5 trauma-informed questions from user self-assessment"""
    safety_baseline: int = Field(..., ge=1, le=10, description="Never felt fundamentally safe (1) vs. Currently safe (10)")
    adhd_wiring: int = Field(..., ge=1, le=10, description="Execution depletes me (1) vs. First-play energizes (10)")
    capability: int = Field(..., ge=1, le=10, description="I discount my skills (1) vs. I own my expertise (10)")
    co_regulation: int = Field(..., ge=1, le=10, description="Need constant external validation (1) vs. Internal compass stable (10)")
    financial: int = Field(..., ge=1, le=10, description="Financial stress is crushing (1) vs. Survival is secure (10)")

def calculate_aoja_score(job_description: str, trauma_input: TraumaScannerInput) -> dict:
    """
    Calculate trauma-informed AOJA score based on job description + user trauma pattern.
    
    This is the CORE ENGINE. It does NOT analyze the job for "autonomy." It asks:
    "Given THIS USER's trauma pattern, how will THIS JOB score on THEIR nervous system?"
    """
    text_lower = job_description.lower()
    
    # 1. Safety_Baseline Score (30% weight)
    # Critical: Does job threaten user-specific safety needs?
    safety_score = 50.0  # baseline
    safety_red_flags = []
    
    # Read user's self-scored safety need
    if trauma_input.safety_baseline <= 5:  # User has LOW safety baseline
        # Any "office required" = massive penalty
        if "on-site" in text_lower or "relocate" in text_lower:
            safety_score = max(0, safety_score - 40)
            safety_red_flags.append("Safety: Requires relocation/on-site (critical for low-safety users)")
        if "micromanagement" in text_lower or "constant supervision" in text_lower:
            safety_score = max(0, safety_score - 30)
            safety_red_flags.append("Safety: Micromanagement language detected")
    
    # Positive safety signals
    if "remote-first" in text_lower or "async-first" in text_lower:
        safety_score = min(100, safety_score + 20)
    
    # 2. ADHD_Wiring Score (20% weight)
    # Critical: Does job require execution vs. diagnostic work?
    adhd_score = 50.0
    adhd_red_flags = []
    
    if trauma_input.adhd_wiring <= 5:  # User is execution-depleted
        # "Post-launch execution" = death sentence
        if "execute" in text_lower and "strategy" not in text_lower:
            adhd_score = max(0, adhd_score - 25)
            adhd_red_flags.append("ADHD: Post-launch execution role (depletion risk)")
        
        # Meeting overload
        meetings = len(re.findall(r'meeting', text_lower))
        if meetings > 5:
            adhd_score = max(0, adhd_score - (meetings * 3))
            adhd_red_flags.append(f"ADHD: {meetings} mentions of meetings (synchronous overload)")
    
    # 3. Capability_Fit Score (25% weight)
    # Does job leverage user's *actual* capabilities (from LinkedIn, not imposter brain)?
    capability_score = 50.0
    
    # For MVP: Hardcode Andrew's capabilities
    user_capabilities = ["systems thinking", "automation", "storytelling"]
    job_capabilities = ["automation" in text_lower, "strategy" in text_lower]
    
    match_ratio = sum(job_capabilities) / len(user_capabilities)
    capability_score = 50 + (match_ratio * 50)
    
    # 4. Co_Regulation Score (15% weight)
    # Does job provide relational safety?
    coreg_score = 50.0
    coreg_red_flags = []
    
    if trauma_input.co_regulation <= 5:  # User needs external validation
        # Solo role = danger
        if "solopreneur" in text_lower and "team" not in text_lower:
            coreg_score = max(0, coreg_score - 20)
            coreg_red_flags.append("Co-Reg: Isolated role (no team for validation)")
        
        # Manager red flags
        if "micromanage" in text_lower or "track your time" in text_lower:
            coreg_score = max(0, coreg_score - 25)
            coreg_red_flags.append("Co-Reg: Manager language suggests insecure attachment")
    
    # 5. Financial_Viability (10% weight)
    financial_score = 50.0
    if "commission only" in text_lower or "unpaid internship" in text_lower:
        financial_score = max(0, financial_score - 30)
    
    # Weighted sum
    total_score = (
        safety_score * 0.30 +
        adhd_score * 0.20 +
        capability_score * 0.25 +
        coreg_score * 0.15 +
        financial_score * 0.10
    )
    
    # Risk level
    if total_score >= 75:
        risk = "green"
        summary = "This job is trauma-safe for your nervous system."
    elif total_score >= 50:
        risk = "yellow"
        summary = "Proceed with caution—negotiate safety parameters before accepting."
    else:
        risk = "red"
        summary = "Predicted collapse in 6 months. Do not take this job unless in financial crisis."
    
    return {
        "aoja_score": round(total_score, 2),
        "breakdown": {
            "safety_baseline": round(safety_score, 2),
            "adhd_wiring": round(adhd_score, 2),
            "capability_fit": round(capability_score, 2),
            "co_regulation": round(coreg_score, 2),
            "financial": round(financial_score, 2)
        },
        "risk_level": risk,
        "red_flags": safety_red_flags + adhd_red_flags + coreg_red_flags,
        "summary": summary
    }


@app.post("/api/scan-job", response_model=JobScanResponse)
async def scan_job(job: JobScanRequest, trauma: TraumaScannerInput):
    """
    NEW: Accepts BOTH job description AND user's trauma self-assessment
    """
    try:
        result = calculate_aoja_score(job.job_description, trauma)
        return JobScanResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    """
  

@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "AOJA Job Scanner",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Detailed health check."""
    return {
        "status": "ok",
        "endpoints": {
            "/api/scan-job": "POST - Analyze job descriptions"
        }
    }


# Example usage for testing
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)