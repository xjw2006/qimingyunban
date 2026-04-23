from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .models import BlacklistEntry, ForumComment, ForumPost, Proposal, Report, SensitiveWord, User, VerificationCode
from .security import hash_password, verify_password
from .services import (
    evaluate_proposal,
    export_proposal_docx,
    export_proposal_pptx,
    generate_proposal_payload,
    is_strong_password,
    is_valid_china_phone,
    is_valid_email,
    issue_verification_code,
    moderate_text,
    proposal_to_dict,
    registration_blacklist_check,
    report_summary,
    seed_defaults,
    serialize_posts_for_ui,
    verify_code,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def flash(request: Request, kind: str, message: str) -> None:
    items = request.session.get("flashes", [])
    items.append({"kind": kind, "message": message})
    request.session["flashes"] = items


def pop_flashes(request: Request) -> list[dict[str, str]]:
    items = request.session.get("flashes", [])
    request.session["flashes"] = []
    return items


def get_current_user_optional(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, int(user_id))


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/login"})
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def render(request: Request, template_name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    db = SessionLocal()
    try:
        current_user = get_current_user_optional(request, db)
    finally:
        db.close()
    merged = {
        "request": request,
        "app_name": settings.app_name,
        "brand_name": settings.brand_name,
        "current_user": current_user,
        "flashes": pop_flashes(request),
        "dev_show_codes": settings.dev_show_codes,
        **context,
    }
    return templates.TemplateResponse(template_name, merged, status_code=status_code)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_defaults(db, hash_password)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_optional(request, db)
    latest_posts = db.execute(select(ForumPost).where(ForumPost.is_hidden.is_(False)).order_by(desc(ForumPost.created_at)).limit(3)).scalars().all()
    my_count = 0
    if current_user:
        my_count = db.execute(select(Proposal).where(Proposal.owner_id == current_user.id)).scalars().all().__len__()
    return render(
        request,
        "index.html",
        {
            "latest_posts": latest_posts,
            "my_count": my_count,
            "llm_mode": settings.llm_mode,
            "verification_delivery": settings.verification_delivery,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", {})


@app.post("/login")
def login_password(
    request: Request,
    account: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    stmt = select(User).where((User.email == account.strip()) | (User.phone == account.strip()))
    user = db.execute(stmt).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        flash(request, "error", "账号或密码错误。")
        return RedirectResponse("/login", status_code=303)
    if user.is_banned:
        flash(request, "error", f"该账号已被封禁。原因：{user.ban_reason or '请联系管理员'}")
        return RedirectResponse("/login", status_code=303)
    request.session["user_id"] = user.id
    flash(request, "success", f"欢迎回来，{user.full_name}。")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/auth/send-email-code")
def send_email_code(
    request: Request,
    email: str = Form(...),
    purpose: str = Form("register"),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if not is_valid_email(email):
        return JSONResponse({"ok": False, "message": "请输入有效邮箱地址。"})
    if purpose == "register":
        exists = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if exists:
            return JSONResponse({"ok": False, "message": "该邮箱已注册。"})
    else:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not user or not user.email_verified:
            return JSONResponse({"ok": False, "message": "该邮箱未注册或尚未完成验证。"})
    result = issue_verification_code(db, email, "email", purpose)
    return JSONResponse(result)


@app.post("/api/auth/send-phone-code")
def send_phone_code(
    request: Request,
    phone: str = Form(...),
    purpose: str = Form("register"),
    db: Session = Depends(get_db),
):
    phone = phone.strip()
    if not is_valid_china_phone(phone):
        return JSONResponse({"ok": False, "message": "请输入中国大陆 11 位手机号。"})
    if purpose == "register":
        exists = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
        if exists:
            return JSONResponse({"ok": False, "message": "该手机号已注册。"})
    else:
        user = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
        if not user or not user.phone_verified:
            return JSONResponse({"ok": False, "message": "该手机号未注册或尚未完成验证。"})
    result = issue_verification_code(db, phone, "phone", purpose)
    return JSONResponse(result)


@app.post("/register")
def register_user(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    email_code: str = Form(...),
    phone_code: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    phone = phone.strip()
    full_name = full_name.strip()
    if not full_name:
        flash(request, "error", "请填写姓名或团队联系人。")
        return RedirectResponse("/register", status_code=303)
    if not is_valid_email(email):
        flash(request, "error", "邮箱格式无效。")
        return RedirectResponse("/register", status_code=303)
    if not is_valid_china_phone(phone):
        flash(request, "error", "手机号格式无效，仅支持中国大陆手机号。")
        return RedirectResponse("/register", status_code=303)
    if password != password_confirm:
        flash(request, "error", "两次输入的密码不一致。")
        return RedirectResponse("/register", status_code=303)
    if not is_strong_password(password):
        flash(request, "error", "密码至少 8 位，且需包含字母和数字。")
        return RedirectResponse("/register", status_code=303)
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() or db.execute(select(User).where(User.phone == phone)).scalar_one_or_none():
        flash(request, "error", "邮箱或手机号已存在。")
        return RedirectResponse("/register", status_code=303)
    allowed, message = registration_blacklist_check(db, email, phone, full_name)
    if not allowed:
        flash(request, "error", message)
        return RedirectResponse("/register", status_code=303)
    email_ok, email_msg = verify_code(db, email, "email", "register", email_code)
    phone_ok, phone_msg = verify_code(db, phone, "phone", "register", phone_code)
    if not email_ok:
        flash(request, "error", f"邮箱验证码校验失败：{email_msg}")
        return RedirectResponse("/register", status_code=303)
    if not phone_ok:
        flash(request, "error", f"手机验证码校验失败：{phone_msg}")
        return RedirectResponse("/register", status_code=303)
    user = User(
        full_name=full_name,
        email=email,
        phone=phone,
        password_hash=hash_password(password),
        email_verified=True,
        phone_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["user_id"] = user.id
    flash(request, "success", "注册成功，已自动登录。")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "register.html", {})


@app.post("/login/code")
def login_by_code(
    request: Request,
    target: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    target = target.strip()
    is_email = is_valid_email(target)
    is_phone = is_valid_china_phone(target)
    if not is_email and not is_phone:
        flash(request, "error", "请输入已注册的邮箱或手机号。")
        return RedirectResponse("/login", status_code=303)
    field = User.email if is_email else User.phone
    user = db.execute(select(User).where(field == target)).scalar_one_or_none()
    if not user:
        flash(request, "error", "账户不存在。")
        return RedirectResponse("/login", status_code=303)
    if user.is_banned:
        flash(request, "error", f"该账号已被封禁。原因：{user.ban_reason or '请联系管理员'}")
        return RedirectResponse("/login", status_code=303)
    ok, message = verify_code(db, target, "email" if is_email else "phone", "login", code)
    if not ok:
        flash(request, "error", message)
        return RedirectResponse("/login", status_code=303)
    request.session["user_id"] = user.id
    flash(request, "success", "验证码登录成功。")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    flash(request, "success", "已退出登录。")
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_user)):
    proposals = db.execute(select(Proposal).where(Proposal.owner_id == current_user.id).order_by(desc(Proposal.updated_at))).scalars().all()
    proposal_cards = [proposal_to_dict(item) for item in proposals]
    return render(request, "dashboard.html", {"proposals": proposal_cards})


@app.get("/proposals/new", response_class=HTMLResponse)
def new_proposal_page(request: Request, current_user: User = Depends(require_user)):
    return render(request, "proposal_form.html", {"proposal": None, "page_title": "新建策划案"})


@app.post("/proposals/new")
def create_proposal(
    request: Request,
    title: str = Form(...),
    industry: str = Form(...),
    problem: str = Form(...),
    solution: str = Form(...),
    market: str = Form(...),
    business_model: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    payload = {
        "title": title.strip(),
        "industry": industry.strip(),
        "problem": problem.strip(),
        "solution": solution.strip(),
        "market": market.strip(),
        "business_model": business_model.strip(),
    }
    if not all(payload.values()):
        flash(request, "error", "请把所有字段填写完整。")
        return RedirectResponse("/proposals/new", status_code=303)
    generated = generate_proposal_payload(payload)
    score = evaluate_proposal(payload, generated)
    proposal = Proposal(
        owner_id=current_user.id,
        title=payload["title"],
        industry=payload["industry"],
        problem=payload["problem"],
        solution=payload["solution"],
        market=payload["market"],
        business_model=payload["business_model"],
        generated_json=json.dumps(generated, ensure_ascii=False),
        score_json=json.dumps(score, ensure_ascii=False),
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    flash(request, "success", "策划案已生成，可继续导出 DOCX / PPTX。")
    return RedirectResponse(f"/proposals/{proposal.id}", status_code=303)


@app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
def proposal_detail(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or (proposal.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="未找到策划案")
    return render(request, "proposal_detail.html", {"proposal": proposal_to_dict(proposal), "owner_can_edit": proposal.owner_id == current_user.id})


@app.get("/proposals/{proposal_id}/edit", response_class=HTMLResponse)
def edit_proposal_page(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or proposal.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="未找到策划案")
    return render(request, "proposal_form.html", {"proposal": proposal_to_dict(proposal), "page_title": "编辑策划案"})


@app.post("/proposals/{proposal_id}/edit")
def edit_proposal(
    proposal_id: int,
    request: Request,
    title: str = Form(...),
    industry: str = Form(...),
    problem: str = Form(...),
    solution: str = Form(...),
    market: str = Form(...),
    business_model: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or proposal.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="未找到策划案")
    payload = {
        "title": title.strip(),
        "industry": industry.strip(),
        "problem": problem.strip(),
        "solution": solution.strip(),
        "market": market.strip(),
        "business_model": business_model.strip(),
    }
    generated = generate_proposal_payload(payload)
    score = evaluate_proposal(payload, generated)
    proposal.title = payload["title"]
    proposal.industry = payload["industry"]
    proposal.problem = payload["problem"]
    proposal.solution = payload["solution"]
    proposal.market = payload["market"]
    proposal.business_model = payload["business_model"]
    proposal.generated_json = json.dumps(generated, ensure_ascii=False)
    proposal.score_json = json.dumps(score, ensure_ascii=False)
    db.add(proposal)
    db.commit()
    flash(request, "success", "策划案已更新，评分拆解也已重新计算。")
    return RedirectResponse(f"/proposals/{proposal.id}", status_code=303)


@app.post("/proposals/{proposal_id}/delete")
def delete_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or proposal.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="未找到策划案")
    db.delete(proposal)
    db.commit()
    flash(request, "success", "策划案已删除。")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/proposals/{proposal_id}/export/docx")
def export_docx(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or (proposal.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="未找到策划案")
    generated = json.loads(proposal.generated_json)
    score = json.loads(proposal.score_json)
    file_path = export_proposal_docx(proposal, generated, score, current_user.full_name)
    return FileResponse(file_path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=file_path.name)


@app.get("/proposals/{proposal_id}/export/pptx")
def export_pptx(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if not proposal or (proposal.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="未找到策划案")
    generated = json.loads(proposal.generated_json)
    score = json.loads(proposal.score_json)
    file_path = export_proposal_pptx(proposal, generated, score)
    return FileResponse(file_path, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation", filename=file_path.name)


@app.get("/forum", response_class=HTMLResponse)
def forum_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_optional(request, db)
    posts = db.execute(
        select(ForumPost)
        .options(joinedload(ForumPost.author), joinedload(ForumPost.comments).joinedload(ForumComment.author))
        .order_by(desc(ForumPost.created_at))
    ).unique().scalars().all()
    post_items = serialize_posts_for_ui(posts, current_user)
    return render(request, "forum.html", {"posts": post_items})


@app.post("/forum/post")
def create_post(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    if current_user.is_banned:
        flash(request, "error", f"账号已被封禁，无法发帖。原因：{current_user.ban_reason or '请联系管理员'}")
        return RedirectResponse("/forum", status_code=303)
    moderated = moderate_text(db, f"{title}\n{content}")
    if moderated["action"] == "reject":
        flash(request, "error", f"发帖失败：{moderated['message']}")
        return RedirectResponse("/forum", status_code=303)
    post = ForumPost(author_id=current_user.id, title=title.strip(), content=content.strip())
    if moderated["action"] == "replace":
        parts = moderated["content"].split("\n", 1)
        post.title = parts[0]
        post.content = parts[1] if len(parts) > 1 else post.content
        flash(request, "warning", "帖子命中替换型敏感词，已自动脱敏后发布。")
    db.add(post)
    db.commit()
    flash(request, "success", "帖子已发布。")
    return RedirectResponse("/forum", status_code=303)


@app.post("/forum/post/{post_id}/comment")
def create_comment(
    post_id: int,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    post = db.get(ForumPost, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="帖子不存在")
    if current_user.is_banned:
        flash(request, "error", f"账号已被封禁，无法评论。原因：{current_user.ban_reason or '请联系管理员'}")
        return RedirectResponse("/forum", status_code=303)
    moderated = moderate_text(db, content)
    if moderated["action"] == "reject":
        flash(request, "error", f"评论失败：{moderated['message']}")
        return RedirectResponse("/forum", status_code=303)
    comment = ForumComment(post_id=post_id, author_id=current_user.id, content=moderated["content"])
    db.add(comment)
    db.commit()
    if moderated["action"] == "replace":
        flash(request, "warning", "评论命中替换型敏感词，已自动脱敏。")
    else:
        flash(request, "success", "评论已发布。")
    return RedirectResponse("/forum", status_code=303)


@app.post("/forum/report")
def report_target(
    request: Request,
    target_type: str = Form(...),
    target_id: int = Form(...),
    reason: str = Form(...),
    detail: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    if target_type not in {"post", "comment"}:
        raise HTTPException(status_code=400, detail="类型错误")
    report = Report(reporter_id=current_user.id, target_type=target_type, target_id=target_id, reason=reason, detail=detail.strip())
    db.add(report)
    db.commit()
    flash(request, "success", "举报已提交，管理员将尽快处理。")
    return RedirectResponse("/forum", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    users = db.execute(select(User).order_by(desc(User.created_at))).scalars().all()
    reports = db.execute(select(Report).order_by(desc(Report.created_at))).scalars().all()
    words = db.execute(select(SensitiveWord).order_by(SensitiveWord.word)).scalars().all()
    blacklist = db.execute(select(BlacklistEntry).order_by(desc(BlacklistEntry.created_at))).scalars().all()
    posts = db.execute(
        select(ForumPost).options(joinedload(ForumPost.author), joinedload(ForumPost.comments).joinedload(ForumComment.author)).order_by(desc(ForumPost.created_at))
    ).unique().scalars().all()
    summary = report_summary(db)
    return render(
        request,
        "admin.html",
        {
            "users": users,
            "reports": reports,
            "words": words,
            "blacklist": blacklist,
            "posts": posts,
            "report_summary": summary,
        },
    )


@app.post("/admin/posts/{post_id}/toggle-hide")
def admin_toggle_post(
    post_id: int,
    request: Request,
    reason: str = Form("违反社区规范"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    post = db.get(ForumPost, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="帖子不存在")
    post.is_hidden = not post.is_hidden
    post.hidden_reason = reason if post.is_hidden else None
    db.add(post)
    db.commit()
    flash(request, "success", "帖子状态已更新。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/comments/{comment_id}/toggle-hide")
def admin_toggle_comment(
    comment_id: int,
    request: Request,
    reason: str = Form("违反社区规范"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    comment = db.get(ForumComment, comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="评论不存在")
    comment.is_hidden = not comment.is_hidden
    comment.hidden_reason = reason if comment.is_hidden else None
    db.add(comment)
    db.commit()
    flash(request, "success", "评论状态已更新。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/toggle-ban")
def admin_toggle_user(
    user_id: int,
    request: Request,
    reason: str = Form("多次违规发言"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.is_admin:
        flash(request, "error", "不能封禁管理员账号。")
        return RedirectResponse("/admin", status_code=303)
    user.is_banned = not user.is_banned
    user.ban_reason = reason if user.is_banned else None
    db.add(user)
    db.commit()
    flash(request, "success", "用户状态已更新。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sensitive")
def admin_add_sensitive(
    request: Request,
    word: str = Form(...),
    level: str = Form("reject"),
    replacement: str = Form("***"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    word = word.strip()
    if not word:
        flash(request, "error", "敏感词不能为空。")
        return RedirectResponse("/admin", status_code=303)
    if db.execute(select(SensitiveWord).where(SensitiveWord.word == word)).scalar_one_or_none():
        flash(request, "warning", "该敏感词已存在。")
        return RedirectResponse("/admin", status_code=303)
    entry = SensitiveWord(word=word, level=level, replacement=replacement)
    db.add(entry)
    db.commit()
    flash(request, "success", "敏感词已添加。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sensitive/{word_id}/delete")
def admin_delete_sensitive(
    word_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    item = db.get(SensitiveWord, word_id)
    if item:
        db.delete(item)
        db.commit()
    flash(request, "success", "敏感词已删除。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/blacklist")
def admin_add_blacklist(
    request: Request,
    entry_type: str = Form(...),
    value: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    value = value.strip()
    if not value:
        flash(request, "error", "黑名单值不能为空。")
        return RedirectResponse("/admin", status_code=303)
    item = BlacklistEntry(entry_type=entry_type, value=value, note=note.strip(), active=True)
    db.add(item)
    db.commit()
    flash(request, "success", "黑名单条目已添加。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/blacklist/{entry_id}/toggle")
def admin_toggle_blacklist(
    entry_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    item = db.get(BlacklistEntry, entry_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    item.active = not item.active
    db.add(item)
    db.commit()
    flash(request, "success", "黑名单状态已更新。")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/reports/{report_id}/mark")
def admin_mark_report(
    report_id: int,
    request: Request,
    status_value: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="举报记录不存在")
    report.status = status_value
    db.add(report)
    db.commit()
    flash(request, "success", "举报状态已更新。")
    return RedirectResponse("/admin", status_code=303)


@app.get("/dev/codes", response_class=HTMLResponse)
def dev_codes_page(request: Request, db: Session = Depends(get_db)):
    if not settings.dev_show_codes:
        raise HTTPException(status_code=404)
    codes = db.execute(select(VerificationCode).order_by(desc(VerificationCode.created_at)).limit(30)).scalars().all()
    return render(request, "dev_codes.html", {"codes": codes})
