import os
import asyncio
import anthropic
import base64
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponseimport os
import asyncio
import base64
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import traceback

# ── Gemini client ────────────────────────────────────────────────────────────

MODEL_NAME = "gemini-2.0-flash"

def get_model():
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set in environment variables.")
    genai.configure(api_key=key)
    return genai.GenerativeModel(MODEL_NAME)

# ── Vision OCR ───────────────────────────────────────────────────────────────

async def extract_code_from_image(image_bytes: bytes, media_type: str = "image/png") -> str:
    def _call():
        model = get_model()
        image_part = {"mime_type": media_type, "data": image_bytes}
        response = model.generate_content([
            image_part,
            "Extract ALL code from this screenshot exactly as written. "
            "Preserve every character and indentation. "
            "Output ONLY the raw code — no explanation, no markdown fences."
        ])
        return response.text.strip()
    return await asyncio.to_thread(_call)

# ── Language detect ──────────────────────────────────────────────────────────

async def detect_language(code: str) -> str:
    def _call():
        model = get_model()
        response = model.generate_content(
            "What programming language is this? Reply with ONLY the language name.\n\n" + code[:1500]
        )
        return response.text.strip()
    return await asyncio.to_thread(_call)

# ── Prompts ──────────────────────────────────────────────────────────────────

def _numbered(code: str) -> str:
    return "\n".join(f"Line {i+1}: {l}" for i, l in enumerate(code.split("\n")))

def prompt1(code: str, lang: str) -> str:
    return f"""You are a senior {lang} engineer and CS educator.
Analyze this {lang} code. Reply using ONLY the tagged sections — no extra text.

CODE:
{_numbered(code)}

LINE_EXPLANATIONS_START
Line 1: [exact code from line 1]
Explanation: [what this line does — 1 sentence, be specific and technical]
Line 2: [exact code]
Explanation: [explanation]
[...repeat for EVERY single line including blank lines and closing braces — never skip any]
LINE_EXPLANATIONS_END

BUG_DETECTION_START
[Strictly check for: syntax errors, undefined variables, off-by-one errors, missing returns,
type mismatches, memory leaks in C/C++, null/undefined access, infinite loops, wrong operators,
logic errors, missing imports, React hook rule violations, async/await misuse, SQL injection risks]

BUG_1: Line [N] — ERROR|WARNING|SUGGESTION
Code: [exact problematic code]
Issue: [clear explanation of the problem]
Fix: [corrected code or concrete approach]

[If no issues at all:]
BUG_NONE: No bugs detected.
BUG_DETECTION_END

CORRECTED_CODE_START
[If ANY bugs exist: output the COMPLETE corrected code with ALL fixes applied.
Mark each corrected line with a short inline comment:  // FIX: reason  (use # FIX: for Python/Ruby/Bash)]
[If no bugs: write exactly: CLEAN]
CORRECTED_CODE_END"""

def prompt2(code: str, lang: str) -> str:
    return f"""You are a senior {lang} engineer. Analyze this code. Reply using ONLY the tagged sections.

CODE:
{_numbered(code)}

DRY_RUN_START
Sample input: [state the sample input you chose — pick a simple realistic example]

STEP_1:
Line: [line number(s)]
Action: [exactly what executes — be specific]
Variables: [name=value, name=value — show ALL variables currently in scope]

STEP_2:
Line: [line number(s)]
Action: [what executes]
Variables: [complete current state of all variables]

[Continue for all key execution steps.
For loops: show iterations 1, 2, 3 fully, then write "...loop continues similarly for remaining items"
Maximum 15 steps total.]
RESULT: [the final return value or printed output]
DRY_RUN_END

TIME_COMPLEXITY_START
[State the Big-O notation. Then in 2 sentences explain which part of the code drives it.]
TIME_COMPLEXITY_END

SPACE_COMPLEXITY_START
[State the Big-O notation. Then in 2 sentences explain what data structure uses the space.]
SPACE_COMPLEXITY_END

SUGGESTIONS_START
1. [Specific actionable improvement with brief reason]
2. [Specific actionable improvement with brief reason]
3. [Specific actionable improvement with brief reason]
4. [Optional improvement]
5. [Optional improvement]
SUGGESTIONS_END"""

# ── Core analysis ─────────────────────────────────────────────────────────────

def _extract(text: str, start: str, end: str) -> str:
    try:
        s = text.index(start) + len(start)
        e = text.index(end)
        return text[s:e].strip()
    except ValueError:
        return ""

def _sync_call(prompt: str) -> str:
    model = get_model()
    response = model.generate_content(prompt)
    return response.text

async def analyze_code(code: str, language: str) -> dict:
    p1 = prompt1(code, language)
    p2 = prompt2(code, language)
    r1, r2 = await asyncio.gather(
        asyncio.to_thread(_sync_call, p1),
        asyncio.to_thread(_sync_call, p2),
    )
    return {
        "line_explanations": _extract(r1, "LINE_EXPLANATIONS_START", "LINE_EXPLANATIONS_END"),
        "bug_detection":     _extract(r1, "BUG_DETECTION_START",     "BUG_DETECTION_END"),
        "corrected_code":    _extract(r1, "CORRECTED_CODE_START",    "CORRECTED_CODE_END"),
        "dry_run":           _extract(r2, "DRY_RUN_START",           "DRY_RUN_END"),
        "time_complexity":   _extract(r2, "TIME_COMPLEXITY_START",   "TIME_COMPLEXITY_END"),
        "space_complexity":  _extract(r2, "SPACE_COMPLEXITY_START",  "SPACE_COMPLEXITY_END"),
        "suggestions":       _extract(r2, "SUGGESTIONS_START",       "SUGGESTIONS_END"),
    }

# ── Q&A ───────────────────────────────────────────────────────────────────────

async def ask_followup(question: str, code: str, language: str, history: list) -> str:
    system_ctx = (
        f"You are a senior {language} engineer helping a student understand their code.\n\n"
        f"Code:\n{code}\n\n"
        "Answer clearly. Reference line numbers when helpful. Max 6 sentences unless a walkthrough is needed."
    )

    # Build Gemini chat history format
    chat_history = []
    for t in history[-8:]:
        if t.get("question"):
            chat_history.append({"role": "user", "parts": [t["question"]]})
        if t.get("answer"):
            chat_history.append({"role": "model", "parts": [t["answer"]]})

    def _call():
        key = os.getenv("GEMINI_API_KEY", "")
        genai.configure(api_key=key)
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=system_ctx)
        chat = model.start_chat(history=chat_history)
        response = chat.send_message(question)
        return response.text.strip()

    return await asyncio.to_thread(_call)

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="CodeVision", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static HTML frontend
PUBLIC_DIR = Path(__file__).parent.parent / "public"
if PUBLIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR)), name="static")

# ── Models ────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    code:             Optional[str] = None
    image_base64:     Optional[str] = None
    image_media_type: Optional[str] = "image/png"
    language:         str           = "python"

class AskRequest(BaseModel):
    question:             str
    code:                 str
    language:             str
    conversation_history: list = []

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the frontend HTML"""
    html_path = PUBLIC_DIR / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return JSONResponse({"message": "CodeVision API v5.0.0 — powered by Gemini", "docs": "/docs"})

@app.get("/health")
async def health():
    key = os.getenv("GEMINI_API_KEY", "")
    ok  = bool(key and len(key) > 10)
    return JSONResponse({
        "ok":         ok,
        "key_prefix": key[:12] + "…" if ok else "NOT SET",
        "model":      MODEL_NAME,
        "version":    "5.0.0",
        "provider":   "Google Gemini",
    })

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    code = (req.code or "").strip()
    if not code and req.image_base64:
        try:
            raw  = base64.b64decode(req.image_base64)
            code = await extract_code_from_image(raw, req.image_media_type or "image/png")
            if not code.strip():
                raise HTTPException(400, "Could not extract code. Use a high-contrast screenshot.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Image extraction failed: {e}")
    if not code:
        raise HTTPException(400, "No code provided.")
    try:
        result = await analyze_code(code, req.language)
        result["extracted_code"] = code if req.image_base64 else None
        return JSONResponse(result)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Analysis error: {e}")

@app.post("/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(400, "Empty question.")
    if not req.code.strip():
        raise HTTPException(400, "No code context.")
    try:
        answer = await ask_followup(
            req.question.strip(),
            req.code.strip(),
            req.language,
            req.conversation_history,
        )
        return JSONResponse({"answer": answer})
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Q&A error: {e}")
from pydantic import BaseModel
from typing import Optional
import traceback

# ── Anthropic client ────────────────────────────────────────────────────────

_client = None

def get_client():
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in Vercel environment variables.")
        _client = anthropic.Anthropic(api_key=key)
    return _client

MODEL_NAME = "claude-sonnet-4-5"

# ── Vision OCR ──────────────────────────────────────────────────────────────

async def extract_code_from_image(image_bytes: bytes, media_type: str = "image/png") -> str:
    def _call():
        client = get_client()
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                    {"type": "text", "text": "Extract ALL code from this screenshot exactly as written. Preserve every character and indentation. Output ONLY the raw code — no explanation, no markdown fences."}
                ],
            }],
        )
        return response.content[0].text.strip()
    return await asyncio.to_thread(_call)

# ── Language detect ─────────────────────────────────────────────────────────

async def detect_language(code: str) -> str:
    def _call():
        client = get_client()
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=64,
            messages=[{"role": "user", "content": "What programming language is this? Reply with ONLY the language name.\n\n" + code[:1500]}],
        )
        return response.content[0].text.strip()
    return await asyncio.to_thread(_call)

# ── Prompts ─────────────────────────────────────────────────────────────────

def _numbered(code: str) -> str:
    return "\n".join(f"Line {i+1}: {l}" for i, l in enumerate(code.split("\n")))

def prompt1(code: str, lang: str) -> str:
    return f"""You are a senior {lang} engineer and CS educator.
Analyze this {lang} code. Reply using ONLY the tagged sections — no extra text.

CODE:
{_numbered(code)}

LINE_EXPLANATIONS_START
Line 1: [exact code from line 1]
Explanation: [what this line does — 1 sentence, be specific and technical]
Line 2: [exact code]
Explanation: [explanation]
[...repeat for EVERY single line including blank lines and closing braces — never skip any]
LINE_EXPLANATIONS_END

BUG_DETECTION_START
[Strictly check for: syntax errors, undefined variables, off-by-one errors, missing returns,
type mismatches, memory leaks in C/C++, null/undefined access, infinite loops, wrong operators,
logic errors, missing imports, React hook rule violations, async/await misuse, SQL injection risks]

BUG_1: Line [N] — ERROR|WARNING|SUGGESTION
Code: [exact problematic code]
Issue: [clear explanation of the problem]
Fix: [corrected code or concrete approach]

[If no issues at all:]
BUG_NONE: No bugs detected.
BUG_DETECTION_END

CORRECTED_CODE_START
[If ANY bugs exist: output the COMPLETE corrected code with ALL fixes applied.
Mark each corrected line with a short inline comment:  // FIX: reason  (use # FIX: for Python/Ruby/Bash)]
[If no bugs: write exactly: CLEAN]
CORRECTED_CODE_END"""

def prompt2(code: str, lang: str) -> str:
    return f"""You are a senior {lang} engineer. Analyze this code. Reply using ONLY the tagged sections.

CODE:
{_numbered(code)}

DRY_RUN_START
Sample input: [state the sample input you chose — pick a simple realistic example]

STEP_1:
Line: [line number(s)]
Action: [exactly what executes — be specific]
Variables: [name=value, name=value — show ALL variables currently in scope]

STEP_2:
Line: [line number(s)]
Action: [what executes]
Variables: [complete current state of all variables]

[Continue for all key execution steps.
For loops: show iterations 1, 2, 3 fully, then write "...loop continues similarly for remaining items"
Maximum 15 steps total.]
RESULT: [the final return value or printed output]
DRY_RUN_END

TIME_COMPLEXITY_START
[State the Big-O notation. Then in 2 sentences explain which part of the code drives it.]
TIME_COMPLEXITY_END

SPACE_COMPLEXITY_START
[State the Big-O notation. Then in 2 sentences explain what data structure uses the space.]
SPACE_COMPLEXITY_END

SUGGESTIONS_START
1. [Specific actionable improvement with brief reason]
2. [Specific actionable improvement with brief reason]
3. [Specific actionable improvement with brief reason]
4. [Optional improvement]
5. [Optional improvement]
SUGGESTIONS_END"""

# ── Core analysis ───────────────────────────────────────────────────────────

def _extract(text: str, start: str, end: str) -> str:
    try:
        s = text.index(start) + len(start)
        e = text.index(end)
        return text[s:e].strip()
    except ValueError:
        return ""

def _sync_call(prompt: str) -> str:
    client = get_client()
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def analyze_code(code: str, language: str) -> dict:
    p1 = prompt1(code, language)
    p2 = prompt2(code, language)
    r1, r2 = await asyncio.gather(
        asyncio.to_thread(_sync_call, p1),
        asyncio.to_thread(_sync_call, p2),
    )
    return {
        "line_explanations": _extract(r1, "LINE_EXPLANATIONS_START", "LINE_EXPLANATIONS_END"),
        "bug_detection":     _extract(r1, "BUG_DETECTION_START",     "BUG_DETECTION_END"),
        "corrected_code":    _extract(r1, "CORRECTED_CODE_START",    "CORRECTED_CODE_END"),
        "dry_run":           _extract(r2, "DRY_RUN_START",           "DRY_RUN_END"),
        "time_complexity":   _extract(r2, "TIME_COMPLEXITY_START",   "TIME_COMPLEXITY_END"),
        "space_complexity":  _extract(r2, "SPACE_COMPLEXITY_START",  "SPACE_COMPLEXITY_END"),
        "suggestions":       _extract(r2, "SUGGESTIONS_START",       "SUGGESTIONS_END"),
    }

# ── Q&A ─────────────────────────────────────────────────────────────────────

async def ask_followup(question: str, code: str, language: str, history: list) -> str:
    system_ctx = (
        f"You are a senior {language} engineer helping a student understand their code.\n\n"
        f"Code:\n{code}\n\n"
        "Answer clearly. Reference line numbers when helpful. Max 6 sentences unless a walkthrough is needed."
    )
    messages = []
    for t in history[-8:]:
        if t.get("question"):
            messages.append({"role": "user", "content": t["question"]})
        if t.get("answer"):
            messages.append({"role": "assistant", "content": t["answer"]})
    messages.append({"role": "user", "content": question})

    def _call():
        client = get_client()
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=1024,
            system=system_ctx,
            messages=messages,
        )
        return response.content[0].text.strip()
    return await asyncio.to_thread(_call)

# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="CodeVision", version="4.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Models ──────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    code:             Optional[str] = None
    image_base64:     Optional[str] = None
    image_media_type: Optional[str] = "image/png"
    language:         str           = "python"

class AskRequest(BaseModel):
    question:             str
    code:                 str
    language:             str
    conversation_history: list = []

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    ok  = bool(key and len(key) > 10)
    return JSONResponse({
        "ok":         ok,
        "key_prefix": key[:12] + "…" if ok else "NOT SET",
        "model":      MODEL_NAME,
        "version":    "4.1.0",
    })

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    code = (req.code or "").strip()
    if not code and req.image_base64:
        try:
            raw  = base64.b64decode(req.image_base64)
            code = await extract_code_from_image(raw, req.image_media_type or "image/png")
            if not code.strip():
                raise HTTPException(400, "Could not extract code. Use a high-contrast screenshot.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Image extraction failed: {e}")
    if not code:
        raise HTTPException(400, "No code provided.")
    try:
        result = await analyze_code(code, req.language)
        result["extracted_code"] = code if req.image_base64 else None
        return JSONResponse(result)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Analysis error: {e}")

@app.post("/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(400, "Empty question.")
    if not req.code.strip():
        raise HTTPException(400, "No code context.")
    try:
        answer = await ask_followup(
            req.question.strip(),
            req.code.strip(),
            req.language,
            req.conversation_history,
        )
        return JSONResponse({"answer": answer})
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Q&A error: {e}")
