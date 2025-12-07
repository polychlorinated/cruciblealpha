from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List

app = FastAPI()

class TraumaInput(BaseModel):
    safety_baseline: int = Field(..., ge=1, le=10)
    adhd_wiring: int = Field(..., ge=1, le=10)
    capability: int = Field(..., ge=1, le=10)
    co_regulation: int = Field(..., ge=1, le=10)
    financial: int = Field(..., ge=1, le=10)

class JobScanRequest(BaseModel):
    job_description: str
    trauma: TraumaInput

class JobScanResponse(BaseModel):
    score: float
    risk_level: str
    summary: str
    red_flags: List[str]

def calculate_score(job_text: str, trauma: TraumaInput) -> dict:
    job = job_text.lower()
    safety_score = 50.0
    red_flags = []
    
    if trauma.safety_baseline <= 5:
        if "on-site" in job or "relocate" in job:
            safety_score = max(0, safety_score - 40)
            red_flags.append("Safety: Requires relocation (critical for low-safety users)")
    
    if "remote-first" in job:
        safety_score = min(100, safety_score + 20)
    
    adhd_score = 50.0
    if trauma.adhd_wiring <= 5:
        if "execute" in job and "strategy" not in job:
            adhd_score = max(0, adhd_score - 25)
            red_flags.append("ADHD: Execution role (depletion risk)")
    
    capability_score = 50.0
    if "automation" in job or "strategy" in job:
        capability_score = 85.0
    
    total = (safety_score * 0.30 + adhd_score * 0.20 + 
             capability_score * 0.25 + 50 * 0.15 + 50 * 0.10)
    
    if total >= 75:
        risk = "green"
        summary = "Trauma-safe job"
    elif total >= 50:
        risk = "yellow"
        summary = "Proceed with caution"
    else:
        risk = "red"
        summary = "Predicted collapse in 6 months"
    
    return {
        "score": round(total, 2),
        "risk_level": risk,
        "summary": summary,
        "red_flags": red_flags
    }

@app.post("/api/scan-job", response_model=JobScanResponse)
async def scan_job(request: JobScanRequest):
    try:
        result = calculate_score(request.job_description, request.trauma)
        return JobScanResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"status": "healthy"}
