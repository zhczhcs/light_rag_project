from typing import List
from pydantic import BaseModel

class DocResponse(BaseModel):
    id: int
    filename: str
    upload_time: str
    file_size: str
    status: str

class ChatRequest(BaseModel):
    query: str
    mode: str = "hybrid"
    session_id: int = None

class Source(BaseModel):
    id: int
    content: str
    content_full: str
    highlight_terms: List[str]
    source_filename: str
    score: float

class ChatResponse(BaseModel):
    answer: str
    sources: List[Source]
