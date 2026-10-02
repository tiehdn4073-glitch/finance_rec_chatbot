import os
import csv
import hashlib
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    Request,
    Form,
    UploadFile,
    File
)

from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse
)

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from openai import OpenAI


# =========================================================
# 기본 설정
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
PROFILE_DIR = STATIC_DIR / "profiles"

DB_PATH = BASE_DIR / "finanfit.db"
CSV_PATH = STATIC_DIR / "예금목록.csv"

STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)
PROFILE_DIR.mkdir(exist_ok=True)

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if OPENAI_API_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY
    )
else:
    openai_client = None


# =========================================================
# FastAPI
# =========================================================

app = FastAPI(
    title="FinanFit",
    version="1.0.0"
)

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static"
)

templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR)
)


# =========================================================
# DB
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,

            name TEXT NOT NULL,
            email TEXT NOT NULL,

            age INTEGER,
            gender TEXT,
            investment_type TEXT,

            job TEXT,
            income INTEGER,

            profile_image TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# 비밀번호
# =========================================================

def hash_password(password: str):

    salt = secrets.token_hex(16)

    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    )

    return salt + "$" + hashed.hex()


def verify_password(
    password: str,
    stored_password: str
):

    try:

        salt, stored_hash = \
            stored_password.split("$")

        hashed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            100000
        )

        return secrets.compare_digest(
            hashed.hex(),
            stored_hash
        )

    except Exception:

        return False


# =========================================================
# 세션
# =========================================================

def create_session(user_id):

    token = secrets.token_urlsafe(32)

    conn = get_db()

    conn.execute(
        """
        INSERT INTO sessions
        (token, user_id)
        VALUES (?, ?)
        """,
        (
            token,
            user_id
        )
    )

    conn.commit()
    conn.close()

    return token


def get_current_user(request: Request):

    token = request.cookies.get(
        "finanfit_session"
    )

    if not token:
        return None

    conn = get_db()

    user = conn.execute(
        """
        SELECT users.*
        FROM users
        JOIN sessions
        ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,)
    ).fetchone()

    conn.close()

    return user


def delete_session(request: Request):

    token = request.cookies.get(
        "finanfit_session"
    )

    if not token:
        return

    conn = get_db()

    conn.execute(
        "DELETE FROM sessions WHERE token = ?",
        (token,)
    )

    conn.commit()
    conn.close()


# =========================================================
# CSV
# =========================================================

def read_products():

    if not CSV_PATH.exists():
        return []

    encodings = [
        "utf-8-sig",
        "cp949",
        "euc-kr"
    ]

    for encoding in encodings:

        try:

            with open(
                CSV_PATH,
                "r",
                encoding=encoding,
                newline=""
            ) as f:

                reader = csv.DictReader(f)

                products = []

                for row in reader:

                    cleaned = {}

                    for key, value in row.items():

                        if key is None:
                            continue

                        clean_key = str(
                            key
                        ).strip()

                        clean_value = (
                            ""
                            if value is None
                            else str(value).strip()
                        )

                        cleaned[
                            clean_key
                        ] = clean_value

                    products.append(cleaned)

                return products

        except UnicodeDecodeError:

            continue

    return []


# =========================================================
# CSV 컬럼 찾기
# =========================================================

def find_value(
    product,
    possible_names
):

    for name in possible_names:

        if name in product:

            value = product[name]

            if value:
                return value

    return ""


def get_product_name(product):

    value = find_value(
        product,
        [
            "상품명",
            "상품 이름",
            "상품이름",
            "상품",
            "예금명",
            "적금명",
            "상품명칭",
            "name",
            "product_name"
        ]
    )

    if value:
        return value

    for key, value in product.items():

        if value:
            return value

    return "금융상품"


def get_company(product):

    value = find_value(
        product,
        [
            "은행명",
            "은행",
            "금융회사",
            "금융기관",
            "회사명",
            "company",
            "bank"
        ]
    )

    return value or "금융기관"


def get_rate(product):

    value = find_value(
        product,
        [
            "최고금리",
            "최고 금리",
            "최대금리",
            "최대 금리",
            "금리",
            "이자율",
            "기본금리",
            "우대금리"
        ]
    )

    return value or "확인 필요"


def get_product_type(product):

    text = " ".join(
        str(value)
        for value in product.values()
    )

    if "적금" in text:
        return "적금"

    if "예금" in text:
        return "예금"

    return "금융상품"


# =========================================================
# 추천 알고리즘
# =========================================================

def calculate_score(
    product,
    user
):

    text = " ".join(
        str(value).lower()
        for value in product.values()
    )

    score = 0

    age = int(
        user["age"] or 0
    )

    income = int(
        user["income"] or 0
    )

    investment_type = (
        user["investment_type"]
        or ""
    )

    job = (
        user["job"]
        or ""
    )

    # -----------------------------------------
    # 투자성향
    # -----------------------------------------

    if investment_type == "안정형":

        if "예금" in text:
            score += 12

        if "적금" in text:
            score += 8

        if "원금" in text:
            score += 3

    elif investment_type == "중립형":

        if "예금" in text:
            score += 8

        if "적금" in text:
            score += 8

    elif investment_type == "공격형":

        if "금리" in text:
            score += 8

        if "우대" in text:
            score += 5

    # -----------------------------------------
    # 나이
    # -----------------------------------------

    if 19 <= age <= 29:

        for keyword in [
            "청년",
            "주니어",
            "첫",
            "사회초년생",
            "2030"
        ]:

            if keyword in text:
                score += 10

    elif 30 <= age <= 49:

        if "직장인" in text:
            score += 8

    elif age >= 50:

        for keyword in [
            "시니어",
            "노후",
            "연금",
            "실버"
        ]:

            if keyword in text:
                score += 10

    # -----------------------------------------
    # 직업
    # -----------------------------------------

    if job:

        if job.lower() in text.lower():
            score += 8

        if "학생" in job and "대학생" in text:
            score += 8

        if "공무원" in job and "공무원" in text:
            score += 8

        if (
            "사업" in job
            and "사업자" in text
        ):
            score += 8

    # -----------------------------------------
    # 소득
    # -----------------------------------------

    if income <= 3000:

        if "청년" in text:
            score += 4

        if "우대" in text:
            score += 3

    elif income >= 7000:

        if "우대" in text:
            score += 3

    # -----------------------------------------
    # 금리
    # -----------------------------------------

    rate_text = get_rate(product)

    try:

        numbers = []

        current = ""

        for char in rate_text:

            if char.isdigit() or char == ".":

                current += char

            else:

                if current:
                    numbers.append(current)
                    current = ""

        if current:
            numbers.append(current)

        if numbers:

            rate = float(
                numbers[0]
            )

            score += min(
                rate * 2,
                10
            )

    except Exception:

        pass

    return score


def recommend_products(
    user,
    count=3
):

    products = read_products()

    if not products:
        return []

    scored_products = []

    for index, product in enumerate(products):

        score = calculate_score(
            product,
            user
        )

        scored_products.append(
            (
                score,
                index,
                product
            )
        )

    scored_products.sort(
        key=lambda item: (
            item[0],
            -item[1]
        ),
        reverse=True
    )

    result = []

    for score, index, product in scored_products[:count]:

        result.append({
            "name": get_product_name(product),
            "company": get_company(product),
            "rate": get_rate(product),
            "type": get_product_type(product),
            "score": score
        })

    return result


# =========================================================
# 상품 텍스트
# =========================================================

def get_product_text():

    products = read_products()

    lines = []

    for product in products[:20]:

        values = []

        for key, value in product.items():

            if value:
                values.append(
                    f"{key}: {value}"
                )

        lines.append(
            " / ".join(values)
        )

    return "\n".join(lines)


# =========================================================
# 홈
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home(request: Request):

    user = get_current_user(request)

    if user:

        return RedirectResponse(
            "/main",
            status_code=303
        )

    return RedirectResponse(
        "/login",
        status_code=303
    )


# =========================================================
# 로그인
# =========================================================

@app.get(
    "/login",
    response_class=HTMLResponse
)
async def login_page(
    request: Request
):

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={}
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    conn = get_db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    conn.close()

    if (
        user is None
        or not verify_password(
            password,
            user["password"]
        )
    ):

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error":
                    "아이디 또는 비밀번호가 올바르지 않습니다."
            },
            status_code=401
        )

    token = create_session(
        user["id"]
    )

    response = RedirectResponse(
        "/main",
        status_code=303
    )

    response.set_cookie(
        key="finanfit_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7
    )

    return response


# =========================================================
# 회원가입
# =========================================================

@app.get(
    "/signup",
    response_class=HTMLResponse
)
async def signup_page(
    request: Request
):

    return templates.TemplateResponse(
        request=request,
        name="signup.html",
        context={}
    )


@app.post("/signup")
async def signup(
    request: Request,

    username: str = Form(...),
    password: str = Form(...),

    name: str = Form(...),
    email: str = Form(...),

    age: int = Form(...),
    gender: str = Form(...),

    investment_type: str = Form(...),

    job: str = Form(...),
    income: int = Form(...),

    profile_image: Optional[
        UploadFile
    ] = File(None)
):

    conn = get_db()

    existing = conn.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    if existing:

        conn.close()

        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={
                "error":
                    "이미 사용 중인 아이디입니다."
            },
            status_code=400
        )

    saved_image = ""

    if (
        profile_image
        and profile_image.filename
    ):

        extension = Path(
            profile_image.filename
        ).suffix.lower()

        allowed_extensions = [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp"
        ]

        if extension in allowed_extensions:

            filename = (
                secrets.token_hex(12)
                + extension
            )

            file_path = (
                PROFILE_DIR / filename
            )

            content = (
                await profile_image.read()
            )

            with open(
                file_path,
                "wb"
            ) as f:

                f.write(content)

            saved_image = (
                "profiles/" + filename
            )

    password_hash = hash_password(
        password
    )

    conn.execute(
        """
        INSERT INTO users
        (
            username,
            password,
            name,
            email,
            age,
            gender,
            investment_type,
            job,
            income,
            profile_image
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            password_hash,
            name,
            email,
            age,
            gender,
            investment_type,
            job,
            income,
            saved_image
        )
    )

    conn.commit()
    conn.close()

    return RedirectResponse(
        "/login?signup=success",
        status_code=303
    )


# =========================================================
# 로그아웃
# =========================================================

@app.get("/logout")
async def logout(
    request: Request
):

    delete_session(request)

    response = RedirectResponse(
        "/login",
        status_code=303
    )

    response.delete_cookie(
        "finanfit_session"
    )

    return response


# =========================================================
# 메인
# =========================================================

@app.get(
    "/main",
    response_class=HTMLResponse
)
async def main_page(
    request: Request
):

    user = get_current_user(request)

    if user is None:

        return RedirectResponse(
            "/login",
            status_code=303
        )

    recommendations = recommend_products(
        user,
        count=3
    )

    return templates.TemplateResponse(
        request=request,
        name="main.html",
        context={
            "user": user,
            "recommendations":
                recommendations
        }
    )


# =========================================================
# 챗봇
# =========================================================

@app.get(
    "/chatbot",
    response_class=HTMLResponse
)
async def chatbot_page(
    request: Request
):

    user = get_current_user(request)

    if user is None:

        return RedirectResponse(
            "/login",
            status_code=303
        )

    return templates.TemplateResponse(
        request=request,
        name="chatbot.html",
        context={
            "user": user
        }
    )


@app.post("/api/chat")
async def api_chat(
    request: Request
):

    user = get_current_user(request)

    if user is None:

        return JSONResponse(
            {
                "answer":
                    "로그인이 필요합니다."
            },
            status_code=401
        )

    data = await request.json()

    message = str(
        data.get(
            "message",
            ""
        )
    ).strip()

    if not message:

        return JSONResponse({
            "answer":
                "궁금한 내용을 입력해주세요."
        })

    # -----------------------------------------
    # API KEY가 없는 경우
    # -----------------------------------------

    if openai_client is None:

        return JSONResponse({
            "answer":
                "OPENAI_API_KEY가 설정되지 않았습니다."
        })

    # -----------------------------------------
    # 상품 데이터
    # -----------------------------------------

    product_text = get_product_text()

    system_prompt = f"""
당신은 FinanFit 금융상품 상담 AI입니다.

사용자 정보:

이름: {user["name"]}
나이: {user["age"]}세
성별: {user["gender"]}
투자성향: {user["investment_type"]}
직업: {user["job"]}
연소득: {user["income"]:,}만원

금융상품 데이터:

{product_text}

상담 규칙:

1. 반드시 제공된 금융상품 데이터를 기준으로 답변합니다.
2. CSV에 없는 금리나 가입조건은 만들어내지 않습니다.
3. 사용자의 나이, 투자성향, 직업, 소득을 고려하여 설명합니다.
4. 특정 금융상품 가입을 무조건 권유하지 않습니다.
5. 상품의 장단점을 쉽게 설명합니다.
6. 모르는 정보는 확인이 필요하다고 말합니다.
7. 한국어로 답변합니다.
8. 답변은 너무 길지 않게 핵심 위주로 작성합니다.
"""

    try:

        response = openai_client.responses.create(
            model="gpt-5.6-luna",
            instructions=system_prompt,
            input=message
        )

        answer = response.output_text

        return JSONResponse({
            "answer": answer
        })

    except Exception as e:

        print(
            "OPENAI ERROR:",
            repr(e)
        )

        return JSONResponse({
            "answer":
                "AI 상담 중 오류가 발생했습니다. "
                "OPENAI API 설정이나 API 사용 가능 모델을 확인해주세요."
        })


# =========================================================
# favicon
# =========================================================

@app.get("/favicon.ico")
async def favicon():

    return RedirectResponse(
        "/static/logo.png"
    )


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=1439,
        reload=True
    )
