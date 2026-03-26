import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import scrape, parse_course_number

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 일단 전체 허용
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




class ScrapeRequest(BaseModel):
    course_id: str  # 예: "2026-1-HUM2038-01"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/scrape")
async def scrape_course(req: ScrapeRequest):
    try:
        parse_course_number(req.course_id)  # 형식 검증
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        data = await asyncio.to_thread(scrape, req.course_id)
        return data
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"스크래핑 실패: {str(e)}")
