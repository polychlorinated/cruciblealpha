from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict
from fastapi.responses import HTMLResponse, JSONResponse
import json

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

class PersonalReportRequest(BaseModel):
    trauma: TraumaInput

class JobScanResponse(BaseModel):
    score: float
    risk_level: str
    summary: str
    red_flags: List[str]

class PersonalReportResponse(BaseModel):
    personal_score: float
    dimensions: Dict[str, float]
    archetype: str
    skill_recommendations: List[str]

def calculate_job_score(job_text: str, trauma: TraumaInput) -> dict:
    """Layer 1: Job-specific scoring based on YOUR trauma pattern"""
    job = job_text.lower()
    
    # Your trauma scores INVERT the baseline (low score = high need)
    safety_weight = (11 - trauma.safety_baseline) / 10  # 1.0 if you have LOW safety
    adhd_weight = (11 - trauma.adhd_wiring) / 10        # 1.0 if execution depletes you
    
    # Job penalties scale by your trauma severity
    safety_penalty = 0
    if "on-site" in job or "relocate" in job:
        safety_penalty = 40 * safety_weight
    
    adhd_penalty = 0
    if "execute" in job and "strategy" not in job:
        adhd_penalty = 25 * adhd_weight
    
    # Base scores start at 50, then apply trauma-scaled penalties
    safety_score = max(0, 50 - safety_penalty)
    adhd_score = max(0, 50 - adhd_penalty)
    capability_score = 50
    if "automation" in job or "strategy" in job:
        capability_score = 85
    
    co_regulation_score = 50
    if "team" in job and "collaborative" in job:
        co_regulation_score = 70
    
    financial_score = 50
    
    # Weighted total (now responsive to YOUR trauma pattern)
    total = (safety_score * 0.30 + adhd_score * 0.20 + 
             capability_score * 0.25 + co_regulation_score * 0.15 + financial_score * 0.10)
    
    # Risk level
    if total >= 75:
        risk = "green"
        summary = "This job is safe for YOUR trauma pattern"
    elif total >= 50:
        risk = "yellow"
        summary = "Proceed with caution—negotiate safety parameters first"
    else:
        risk = "red"
        summary = "Predicted collapse in 6 months for YOUR nervous system"
    
    return {
        "score": round(total, 2),
        "risk_level": risk,
        "summary": summary,
        "red_flags": ["Safety penalty: " + str(safety_penalty)[:4], "ADHD penalty: " + str(adhd_penalty)[:4]] if safety_penalty > 0 or adhd_penalty > 0 else []
    }

def calculate_personal_report(trauma: TraumaInput) -> dict:
    """Layer 2: Personal AOJA score based only on your trauma pattern"""
    dimensions = {
        "Safety Baseline": trauma.safety_baseline * 10,
        "ADHD Wiring": trauma.adhd_wiring * 10,
        "Capability": trauma.capability * 10,
        "Co-regulation": trauma.co_regulation * 10,
        "Financial": trauma.financial * 10
    }
    
    personal_score = sum(dimensions.values()) / 5
    
    # Archetype based on lowest scores
    lowest = min(dimensions, key=dimensions.get)
    archetype = "Strategic Cartographer" if "Capability" in lowest else "Safety-First Navigator"
    
    return {
        "personal_score": round(personal_score, 2),
        "dimensions": dimensions,
        "archetype": archetype,
        "skill_recommendations": ["Learn n8n automation", "Build co-regulation network"]
    }

@app.post("/api/scan-job", response_model=JobScanResponse)
async def scan_job(request: JobScanRequest):
    try:
        result = calculate_job_score(request.job_description, request.trauma)
        return JobScanResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/personal-report", response_model=PersonalReportResponse)
async def personal_report(request: PersonalReportRequest):
    """Generate personal AOJA report based on survey alone"""
    try:
        result = calculate_personal_report(request.trauma)
        return PersonalReportResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("index.html", "r") as f:
        return f.read()

@app.get("/health")
async def health():
    return {"status": "healthy"}
