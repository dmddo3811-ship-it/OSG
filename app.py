import os
import json
import random
import smtplib
import hashlib
from contextlib import contextmanager
from email.mime.text import MIMEText
from datetime import datetime

import psycopg2
from flask import Flask, request, jsonify, Response

# ==========================================
# 📌 0. 관리자 계정, 메일 및 발신 계정 설정
#    (실서비스에서는 아래 값들을 하드코딩하지 말고
#     Render 대시보드의 Environment 변수로 설정하세요.
#     여기 있는 기본값은 환경변수가 없을 때만 쓰이는 폴백입니다.)
# ==========================================
ADMIN_EMAILS = [e.strip() for e in os.environ.get(
    "ADMIN_EMAILS", "2510326@saerom.hs.kr,2510924@saerom.hs.kr"
).split(",") if e.strip()]
ADMIN_LOGIN_ID = os.environ.get("ADMIN_LOGIN_ID", "OSG_admin")
ADMIN_LOGIN_PASSWORD = os.environ.get("ADMIN_LOGIN_PASSWORD", "qwerty4321!")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onlysaerom1@gmail.com")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "zmjlsoswesyvtkpj")

# 📌 Supabase의 "Connection Pooling" 접속 문자열(6543 포트, Transaction 모드)을 사용하세요.
#    Render처럼 요청마다 커넥션을 짧게 열고 닫는 서비스에는 pooler 접속이 훨씬 안전합니다.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL 환경변수가 설정되지 않았습니다. "
        "Supabase 프로젝트의 Connection string(Session/Transaction pooler)을 "
        "Render 서비스의 Environment 변수로 등록해주세요."
    )

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 업로드 이미지 등 요청 본문 최대 12MB


@contextmanager
def db_cursor(commit=False):
    """요청마다 새 커넥션을 열고 끝나면 반드시 닫는다 (Supabase pooler와 궁합이 좋음)."""
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def init_db():
    with db_cursor(commit=True) as c:
        c.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            author_id TEXT NOT NULL DEFAULT '101',
            password TEXT NOT NULL,
            created_at TEXT NOT NULL,
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            is_concept INTEGER DEFAULT 0,
            grade TEXT DEFAULT '1',
            gallery TEXT DEFAULT 'all',
            image_url TEXT DEFAULT '',
            views INTEGER DEFAULT 0
        )
        ''')
        c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            author_id TEXT NOT NULL DEFAULT '101',
            password TEXT NOT NULL,
            created_at TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
        )
        ''')
        c.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        ''')
        c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            student_id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            grade TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        ''')
        c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')
        for col, coltype in [("author_id", "TEXT DEFAULT '101'"), ("is_concept", "INTEGER DEFAULT 0"),
                              ("grade", "TEXT DEFAULT '1'"), ("gallery", "TEXT DEFAULT 'all'"),
                              ("image_url", "TEXT DEFAULT ''"), ("views", "INTEGER DEFAULT 0")]:
            c.execute(f"ALTER TABLE posts ADD COLUMN IF NOT EXISTS {col} {coltype}")
        for col, coltype in [("author_id", "TEXT DEFAULT '101'"), ("image_url", "TEXT DEFAULT ''")]:
            c.execute(f"ALTER TABLE comments ADD COLUMN IF NOT EXISTS {col} {coltype}")
        # 📌 관리자 권한은 이메일 인증 계정이 아닌 별도의 관리자 로그인(ADMIN_LOGIN_ID)으로만 부여됩니다.
        c.execute("UPDATE users SET is_admin = 0")


init_db()


# ==========================================
# 📌 1. 비즈니스 로직 (기존 Colab 버전과 동일한 함수들)
# ==========================================
def send_email_py(to_email):
    if not to_email.endswith('@saerom.hs.kr'):
        return {"success": False, "msg": "새롬고 이메일(@saerom.hs.kr)만 사용 가능합니다."}
    student_id = to_email.split('@')[0]
    prefix = student_id[:2]
    is_admin = False
    if prefix == "26": assigned_grade = "1"
    elif prefix == "25": assigned_grade = "2"
    elif prefix == "24": assigned_grade = "3"
    else: return {"success": False, "msg": "인증 불가: 24(3학년), 25(2학년), 26(1학년) 학번만 가능합니다."}
    auth_code = str(random.randint(100000, 999999))
    try:
        msg = MIMEText(f"온리새롬 갤러리 학생 인증번호는 [{auth_code}] 입니다.\n인증 완료 시 {assigned_grade}학년 갤러리 이용이 가능합니다.")
        msg['Subject'] = "[온리새롬] 학생 인증번호 안내"
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [to_email], msg.as_string())
        server.quit()
        return {"success": True, "code": auth_code, "grade": assigned_grade, "is_admin": is_admin, "msg": "인증번호가 이메일로 발송되었습니다."}
    except Exception as e:
        # 📌 실제 서비스에서는 발송 실패 시 인증번호를 절대 클라이언트로 내려보내지 않습니다
        #    (그렇게 하면 이메일 인증을 우회해서 아무나 계정을 만들 수 있게 됩니다).
        return {
            "success": False,
            "msg": "인증번호 메일 발송에 실패했습니다. 잠시 후 다시 시도하거나 관리자에게 문의해주세요."
        }


def _hash_pw(password):
    return hashlib.sha256((password or '').encode('utf-8')).hexdigest()


def _is_verified_user(student_id):
    if student_id == ADMIN_LOGIN_ID:
        return True
    try:
        with db_cursor() as c:
            c.execute('SELECT 1 FROM users WHERE student_id = %s', (student_id,))
            return c.fetchone() is not None
    except Exception:
        return False


def create_account_py(email, password):
    try:
        if not email.endswith('@saerom.hs.kr'):
            return {"success": False, "msg": "새롬고 이메일(@saerom.hs.kr)만 사용 가능합니다."}
        if not password or len(password) < 4:
            return {"success": False, "msg": "비밀번호는 4자 이상 입력해주세요."}
        student_id = email.split('@')[0]
        prefix = student_id[:2]
        is_admin = False
        if prefix == "26": assigned_grade = "1"
        elif prefix == "25": assigned_grade = "2"
        elif prefix == "24": assigned_grade = "3"
        else: return {"success": False, "msg": "인증 불가: 24(3학년), 25(2학년), 26(1학년) 학번만 가능합니다."}
        with db_cursor(commit=True) as c:
            c.execute('''
                INSERT INTO users (student_id, password_hash, grade, is_admin, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (student_id) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    grade = EXCLUDED.grade,
                    is_admin = EXCLUDED.is_admin,
                    created_at = EXCLUDED.created_at
            ''', (student_id, _hash_pw(password), assigned_grade, 1 if is_admin else 0,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return {"success": True, "grade": assigned_grade, "is_admin": is_admin, "msg": "계정이 생성되었습니다."}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def login_py(student_id, password):
    try:
        student_id = (student_id or '').strip()
        if student_id == ADMIN_LOGIN_ID and password == ADMIN_LOGIN_PASSWORD:
            return {"success": True, "grade": "admin", "is_admin": True, "msg": "관리자로 로그인되었습니다."}
        with db_cursor() as c:
            c.execute('SELECT password_hash, grade, is_admin FROM users WHERE student_id = %s', (student_id,))
            row = c.fetchone()
        if not row:
            return {"success": False, "msg": "등록된 계정이 없습니다. 먼저 학번 메일 인증으로 계정을 만들어주세요."}
        if row[0] != _hash_pw(password):
            return {"success": False, "msg": "비밀번호가 일치하지 않습니다."}
        return {"success": True, "grade": row[1], "is_admin": bool(row[2]), "msg": "로그인되었습니다."}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def change_password_py(student_id, old_password, new_password):
    try:
        student_id = (student_id or '').strip()
        if student_id == ADMIN_LOGIN_ID:
            return {"success": False, "msg": "관리자 계정은 비밀번호를 변경할 수 없습니다."}
        if not new_password or len(new_password) < 4:
            return {"success": False, "msg": "새 비밀번호는 4자 이상 입력해주세요."}
        with db_cursor(commit=True) as c:
            c.execute('SELECT password_hash FROM users WHERE student_id = %s', (student_id,))
            row = c.fetchone()
            if not row:
                return {"success": False, "msg": "등록된 계정이 없습니다."}
            if row[0] != _hash_pw(old_password):
                return {"success": False, "msg": "현재 비밀번호가 일치하지 않습니다."}
            c.execute('UPDATE users SET password_hash = %s WHERE student_id = %s', (_hash_pw(new_password), student_id))
        return {"success": True, "msg": "비밀번호가 변경되었습니다."}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def send_admin_request_py(req_category, sender_contact, content):
    try:
        if not content.strip():
            return {"success": False, "msg": "요청 내용을 입력해주세요."}
        subject = f"[온리새롬 문의] [{req_category}] 요청이 접수되었습니다."
        body = f"온리새롬 갤러리에 새로운 관리자 요청이 접수되었습니다.\n\n" \
               f"▪ 요청 카테고리: {req_category}\n" \
               f"▪ 작성자/연락처: {sender_contact or '미기입'}\n" \
               f"▪ 접수 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n" \
               f"----------------------------------------\n" \
               f"[요청 내용]\n{content}\n" \
               f"----------------------------------------"
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = ", ".join(ADMIN_EMAILS)
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, ADMIN_EMAILS, msg.as_string())
        server.quit()
        return {"success": True, "msg": "관리자에게 성공적으로 전달되었습니다."}
    except Exception as e:
        return {"success": False, "msg": f"발송 중 오류 발생: {str(e)}"}


def get_posts_py(tab_type='all', sort_type='date', gallery_type='all', search_kw=''):
    try:
        conditions = []
        params = []
        if tab_type == 'concept':
            conditions.append("is_concept = 1")
            if gallery_type == 'all_global':
                conditions.append("gallery IN ('all', 'dating', 'study', 'overseas_fb', 'admin_notice', 'g1', 'g2', 'g3')")
            else:
                conditions.append("gallery = %s")
                params.append(gallery_type)
        else:
            if gallery_type == 'all_global':
                conditions.append("gallery = 'all'")
            elif gallery_type != 'all':
                conditions.append("gallery = %s")
                params.append(gallery_type)
        if search_kw.strip():
            conditions.append("(title LIKE %s OR content LIKE %s)")
            kw = f"%{search_kw.strip()}%"
            params.extend([kw, kw])
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        if sort_type == 'upvotes': order_clause = "ORDER BY upvotes DESC, id DESC"
        elif sort_type == 'comments': order_clause = "ORDER BY comment_count DESC, id DESC"
        elif sort_type == 'views': order_clause = "ORDER BY views DESC, id DESC"
        else: order_clause = "ORDER BY id DESC"
        query = f"""
        SELECT id, title, author, author_id, created_at,
               (SELECT COUNT(*) FROM comments WHERE post_id = posts.id) AS comment_count,
               upvotes, is_concept, grade, gallery, image_url,
               ROW_NUMBER() OVER (PARTITION BY gallery ORDER BY id ASC) AS local_id
        FROM posts {where_clause} {order_clause}
        """
        with db_cursor() as c:
            c.execute(query, params)
            rows = c.fetchall()
        posts = []
        for r in rows:
            try: date_str = datetime.strptime(r[4], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
            except Exception: date_str = r[4]
            posts.append({
                "id": r[0], "title": r[1], "author": r[2], "author_id": r[3],
                "date": date_str, "comment_count": r[5], "upvotes": r[6],
                "is_concept": r[7], "grade": r[8], "gallery": r[9], "has_image": bool(r[10]),
                "local_id": r[11]
            })
        return {"success": True, "posts": posts}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def get_home_summary_py():
    try:
        def fetch_category(where_sql, limit=4):
            q = f"""
            SELECT id, title, author, created_at,
                   (SELECT COUNT(*) FROM comments WHERE post_id = posts.id) AS comment_count,
                   upvotes, is_concept, gallery,
                   ROW_NUMBER() OVER (PARTITION BY gallery ORDER BY id ASC) AS local_id
            FROM posts {where_sql} ORDER BY id DESC LIMIT {limit}
            """
            with db_cursor() as c:
                c.execute(q)
                rows = c.fetchall()
            res = []
            for r in rows:
                try: d = datetime.strptime(r[3], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
                except Exception: d = r[3]
                res.append({"id": r[0], "title": r[1], "author": r[2], "date": d, "comment_count": r[4], "upvotes": r[5], "is_concept": r[6], "gallery": r[7], "local_id": r[8]})
            return res
        concepts = fetch_category("WHERE is_concept = 1")
        studies = fetch_category("WHERE gallery = 'study'")
        overseas = fetch_category("WHERE gallery = 'overseas_fb'")
        recents = fetch_category("WHERE gallery = 'all'")
        return {"success": True, "concepts": concepts, "studies": studies, "overseas": overseas, "recents": recents}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def get_post_detail_py(post_id, increment_view=True):
    try:
        with db_cursor(commit=True) as c:
            # 📌 조회수는 사용자가 글을 처음 열 때만 올라가야 하므로, 5초마다 도는
            #    실시간 댓글/추천수 폴링 요청(increment_view=False)에서는 올리지 않습니다.
            if increment_view:
                c.execute('UPDATE posts SET views = views + 1 WHERE id = %s', (post_id,))
            c.execute('SELECT id, title, content, author, author_id, created_at, upvotes, downvotes, is_concept, grade, gallery, image_url, views FROM posts WHERE id = %s', (post_id,))
            p = c.fetchone()
            if not p:
                return {"success": False, "msg": "글을 찾을 수 없습니다."}
            c.execute('SELECT id, author, author_id, content, created_at, image_url FROM comments WHERE post_id = %s ORDER BY id ASC', (post_id,))
            cms = c.fetchall()
        comments = []
        for cm in cms:
            try: c_date = datetime.strptime(cm[4], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
            except Exception: c_date = cm[4]
            comments.append({"id": cm[0], "author": cm[1], "author_id": cm[2], "content": cm[3], "date": c_date, "image_url": cm[5]})
        try: p_date = datetime.strptime(p[5], "%Y-%m-%d %H:%M:%S").strftime("%Y.%m.%d %H:%M")
        except Exception: p_date = p[5]
        return {
            "success": True,
            "post": {"id": p[0], "title": p[1], "content": p[2], "author": p[3], "author_id": p[4], "date": p_date, "upvotes": p[6], "downvotes": p[7], "is_concept": p[8], "grade": p[9], "gallery": p[10], "image_url": p[11], "views": p[12]},
            "comments": comments
        }
    except Exception as e:
        return {"success": False, "msg": str(e)}


def add_post_py(title, content, author, author_id, password, gallery, image_url='', is_admin=False):
    try:
        if not title or not content: return {"success": False, "msg": "제목과 내용을 입력하세요."}
        if not _is_verified_user(author_id):
            return {"success": False, "msg": "글쓰기는 로그인 후 이용 가능합니다. 학번 인증으로 계정을 만들고 로그인해주세요."}
        if gallery == 'admin_notice' and not is_admin and author_id != ADMIN_LOGIN_ID:
            return {"success": False, "msg": "관리자 채널은 관리자만 작성할 수 있습니다."}
        # 📌 도배(글 테러) 방지: 관리자가 아니면 계정당 10분에 최대 7개까지만 글 작성 가능
        if not is_admin and author_id != ADMIN_LOGIN_ID:
            with db_cursor() as c:
                c.execute('SELECT created_at FROM posts WHERE author_id = %s ORDER BY id DESC LIMIT 20', (author_id,))
                recent_rows = c.fetchall()
            now = datetime.now()
            recent_count = 0
            for r in recent_rows:
                try:
                    if (now - datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")).total_seconds() <= 600:
                        recent_count += 1
                except Exception:
                    pass
            if recent_count >= 7:
                return {"success": False, "msg": "도배 방지를 위해 10분에 최대 7개까지만 글을 작성할 수 있습니다. 잠시 후 다시 시도해주세요."}
        grade_val = gallery.replace('g', '') if gallery in ['g1', 'g2', 'g3'] else 'all'
        with db_cursor(commit=True) as c:
            c.execute('''
                INSERT INTO posts (title, content, author, author_id, password, created_at, upvotes, downvotes, is_concept, grade, gallery, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 0, %s, %s, %s)
            ''', (title.strip(), content.strip(), author or "ㅇㅇ", author_id or "101", password or "1234",
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), grade_val, gallery or "all", image_url))
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def edit_post_py(post_id, title, content, author_id, image_url='', is_admin=False):
    try:
        if not title or not content: return {"success": False, "msg": "제목과 내용을 입력하세요."}
        with db_cursor(commit=True) as c:
            c.execute('SELECT author_id FROM posts WHERE id = %s', (post_id,))
            row = c.fetchone()
            if not row:
                return {"success": False, "msg": "게시글이 존재하지 않습니다."}
            if row[0] != author_id and not is_admin and author_id != ADMIN_LOGIN_ID:
                return {"success": False, "msg": "본인이 작성한 글만 수정할 수 있습니다."}
            if image_url:
                c.execute('UPDATE posts SET title = %s, content = %s, image_url = %s WHERE id = %s', (title.strip(), content.strip(), image_url, post_id))
            else:
                c.execute('UPDATE posts SET title = %s, content = %s WHERE id = %s', (title.strip(), content.strip(), post_id))
        return {"success": True, "msg": "게시글이 수정되었습니다."}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def get_posts_by_ids_py(id_list):
    try:
        if not id_list:
            return {"success": True, "posts": []}
        placeholders = ','.join(['%s'] * len(id_list))
        with db_cursor() as c:
            c.execute(f"""
                SELECT id, title, author, author_id, created_at,
                       (SELECT COUNT(*) FROM comments WHERE post_id = posts.id) AS comment_count,
                       upvotes, is_concept, grade, gallery, image_url
                FROM posts WHERE id IN ({placeholders}) ORDER BY id DESC
            """, id_list)
            rows = c.fetchall()
        posts = []
        for r in rows:
            try: date_str = datetime.strptime(r[4], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
            except Exception: date_str = r[4]
            posts.append({
                "id": r[0], "title": r[1], "author": r[2], "author_id": r[3],
                "date": date_str, "comment_count": r[5], "upvotes": r[6],
                "is_concept": r[7], "grade": r[8], "gallery": r[9], "has_image": bool(r[10]),
                "local_id": r[0]
            })
        return {"success": True, "posts": posts}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def get_ad_banner_py(slot=1):
    try:
        with db_cursor() as c:
            c.execute('SELECT value FROM settings WHERE key = %s', (f'ad_banner_{slot}',))
            row = c.fetchone()
        return {"success": True, "image_url": row[0] if row else ''}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def set_ad_banner_py(slot, image_url, author_id, is_admin=False):
    try:
        if not is_admin and author_id != ADMIN_LOGIN_ID:
            return {"success": False, "msg": "관리자만 광고 이미지를 등록할 수 있습니다."}
        with db_cursor(commit=True) as c:
            c.execute('''
                INSERT INTO settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            ''', (f'ad_banner_{slot}', image_url))
        return {"success": True, "msg": f"광고란 {slot}에 이미지가 등록되었습니다."}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def delete_post_py(post_id, author_id='', is_admin=False):
    # 📌 예전에는 글마다 정해둔 비밀번호(기본값 '1234')로 아무나 지울 수 있었습니다.
    #    이제 글쓰기 자체가 로그인 필수이므로, 삭제도 edit_post_py와 동일하게
    #    "작성자 본인 또는 관리자"만 가능하도록 소유권으로 검사합니다.
    try:
        with db_cursor(commit=True) as c:
            c.execute('SELECT author_id FROM posts WHERE id = %s', (post_id,))
            row = c.fetchone()
            if not row:
                return {"success": False, "msg": "게시글이 존재하지 않습니다."}
            if row[0] != author_id and not is_admin and author_id != ADMIN_LOGIN_ID:
                return {"success": False, "msg": "본인이 작성한 글만 삭제할 수 있습니다."}
            c.execute('DELETE FROM posts WHERE id = %s', (post_id,))
            c.execute('DELETE FROM comments WHERE post_id = %s', (post_id,))
            msg = "[👑 관리자 권한] 게시글 삭제 완료." if is_admin else "게시글이 삭제되었습니다."
            return {"success": True, "msg": msg}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def add_comment_py(post_id, content, author, author_id, password, image_url=''):
    try:
        if not content and not image_url: return {"success": False, "msg": "내용이나 사진을 첨부하세요."}
        if not _is_verified_user(author_id):
            return {"success": False, "msg": "댓글 작성은 로그인 후 이용 가능합니다. 학번 인증으로 계정을 만들고 로그인해주세요."}
        with db_cursor(commit=True) as c:
            c.execute('''
                INSERT INTO comments (post_id, content, author, author_id, password, created_at, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (post_id, content.strip(), author or "ㅇㅇ", author_id or "101", password or "1234",
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), image_url))
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def vote_post_py(post_id, vote_type):
    try:
        with db_cursor(commit=True) as c:
            if vote_type == 'up':
                c.execute('SELECT created_at, upvotes, is_concept FROM posts WHERE id = %s', (post_id,))
                row = c.fetchone()
                if row:
                    new_up, is_concept = row[1] + 1, row[2]
                    new_is_concept = is_concept
                    if is_concept == 0 and new_up >= 10:
                        try:
                            if (datetime.now() - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")).total_seconds() <= 600:
                                new_is_concept = 1
                        except Exception: new_is_concept = 1
                    c.execute('UPDATE posts SET upvotes = upvotes + 1, is_concept = %s WHERE id = %s', (new_is_concept, post_id))
            elif vote_type == 'down':
                c.execute('UPDATE posts SET downvotes = downvotes + 1 WHERE id = %s', (post_id,))
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def report_item_py(target_type, target_id, reason="사용자 신고"):
    try:
        with db_cursor(commit=True) as c:
            c.execute('INSERT INTO reports (target_type, target_id, reason, created_at) VALUES (%s, %s, %s, %s)',
                      (target_type, target_id, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"[온리새롬 신고 접수] {target_type.upper()} 번호 #{target_id}"
        body = f"온리새롬 갤러리에 새로운 신고가 접수되었습니다.\n\n" \
               f"▪ 신고 대상: {target_type}\n" \
               f"▪ 대상 ID: {target_id}\n" \
               f"▪ 신고 사유: {reason}\n" \
               f"▪ 접수 시각: {report_time}\n"
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = ", ".join(ADMIN_EMAILS)
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, ADMIN_EMAILS, msg.as_string())
        server.quit()
        return {"success": True, "msg": "신고가 접수되어 관리자 이메일로 전송되었습니다."}
    except Exception as e:
        return {"success": True, "msg": f"신고 DB 접수 완료. (메일 발송 안내: {str(e)})"}


# ==========================================
# 📌 2. API 라우트 (프론트엔드 fetch()가 호출하는 엔드포인트)
#    프론트는 각 함수의 인자를 그대로 JSON 배열로 보내고,
#    여기서는 그 배열을 순서대로 풀어서 각 함수에 전달합니다.
# ==========================================
def _args():
    data = request.get_json(silent=True)
    return data if isinstance(data, list) else []


@app.route('/api/send_email', methods=['POST'])
def api_send_email():
    a = _args()
    return jsonify(send_email_py(a[0] if len(a) > 0 else ''))


@app.route('/api/create_account', methods=['POST'])
def api_create_account():
    a = _args()
    return jsonify(create_account_py(a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else ''))


@app.route('/api/login', methods=['POST'])
def api_login():
    a = _args()
    return jsonify(login_py(a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else ''))


@app.route('/api/change_password', methods=['POST'])
def api_change_password():
    a = _args()
    return jsonify(change_password_py(a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else ''))


@app.route('/api/send_admin_request', methods=['POST'])
def api_send_admin_request():
    a = _args()
    return jsonify(send_admin_request_py(a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else ''))


@app.route('/api/get_posts', methods=['POST'])
def api_get_posts():
    a = _args()
    return jsonify(get_posts_py(
        a[0] if len(a) > 0 else 'all',
        a[1] if len(a) > 1 else 'date',
        a[2] if len(a) > 2 else 'all',
        a[3] if len(a) > 3 else ''
    ))


@app.route('/api/get_home_summary', methods=['POST'])
def api_get_home_summary():
    return jsonify(get_home_summary_py())


@app.route('/api/get_post_detail', methods=['POST'])
def api_get_post_detail():
    a = _args()
    return jsonify(get_post_detail_py(a[0] if len(a) > 0 else None, a[1] if len(a) > 1 else True))


@app.route('/api/add_post', methods=['POST'])
def api_add_post():
    a = _args()
    return jsonify(add_post_py(
        a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else '',
        a[3] if len(a) > 3 else '', a[4] if len(a) > 4 else '', a[5] if len(a) > 5 else 'all',
        a[6] if len(a) > 6 else '', a[7] if len(a) > 7 else False
    ))


@app.route('/api/edit_post', methods=['POST'])
def api_edit_post():
    a = _args()
    return jsonify(edit_post_py(
        a[0] if len(a) > 0 else None, a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else '',
        a[3] if len(a) > 3 else '', a[4] if len(a) > 4 else '', a[5] if len(a) > 5 else False
    ))


@app.route('/api/get_posts_by_ids', methods=['POST'])
def api_get_posts_by_ids():
    a = _args()
    return jsonify(get_posts_by_ids_py(a[0] if len(a) > 0 else []))


@app.route('/api/get_ad_banner', methods=['POST'])
def api_get_ad_banner():
    a = _args()
    return jsonify(get_ad_banner_py(a[0] if len(a) > 0 else 1))


@app.route('/api/set_ad_banner', methods=['POST'])
def api_set_ad_banner():
    a = _args()
    return jsonify(set_ad_banner_py(
        a[0] if len(a) > 0 else 1, a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else '', a[3] if len(a) > 3 else False
    ))


@app.route('/api/delete_post', methods=['POST'])
def api_delete_post():
    a = _args()
    return jsonify(delete_post_py(a[0] if len(a) > 0 else None, a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else False))


@app.route('/api/add_comment', methods=['POST'])
def api_add_comment():
    a = _args()
    return jsonify(add_comment_py(
        a[0] if len(a) > 0 else None, a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else '',
        a[3] if len(a) > 3 else '', a[4] if len(a) > 4 else '', a[5] if len(a) > 5 else ''
    ))


@app.route('/api/vote_post', methods=['POST'])
def api_vote_post():
    a = _args()
    return jsonify(vote_post_py(a[0] if len(a) > 0 else None, a[1] if len(a) > 1 else ''))


@app.route('/api/report_item', methods=['POST'])
def api_report_item():
    a = _args()
    return jsonify(report_item_py(a[0] if len(a) > 0 else '', a[1] if len(a) > 1 else None, a[2] if len(a) > 2 else '사용자 신고'))


@app.route('/')
def index():
    return Response(HTML_PAGE, mimetype='text/html')


INTRO_VIDEO_B64 = "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAA4abW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAD6AAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAADUV0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAD6AAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAABVYAAAMAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAA+gAAACAAABAAAAAAy9bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAA8AAAA8ABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAMaG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAADChzdGJsAAAAsHN0c2QAAAAAAAAAAQAAAKBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAABVYDAABIAAAASAAAAAAAAAABFUxhdmM2MS4xOS4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAq/+EAGWdkACqs2UBWBh5uEAAAAwAQAAAHgPGDGWABAAZo6+Ccsiz9+PgAAAAAFGJ0cnQAAAAAAAOMWgAAAAAAAAAYc3R0cwAAAAAAAAABAAAA8AAAAQAAAAAUc3RzcwAAAAAAAAABAAAAAQAAB0BjdHRzAAAAAAAAAOYAAAABAAACAAAAAAEAAAQAAAAAAgAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAMAAAAAAQAAAQAAAAABAAACAAAAAAEAAAMAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAMAAAAAAQAAAQAAAAABAAAEAAAAAAIAAAEAAAAAAQAAAgAAAAABAAADAAAAAAEAAAEAAAAAAQAAAwAAAAABAAABAAAAAAQAAAIAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAQAAAAAAgAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAAAQAABQAAAAABAAACAAAAAAEAAAAAAAAAAQAAAQAAAAABAAAFAAAAAAEAAAIAAAAAAQAAAAAAAAABAAABAAAAAAEAAAUAAAAAAQAAAgAAAAABAAAAAAAAAAEAAAEAAAAA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"

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * { box-sizing: border-box; letter-spacing: -0.3px; }
    html, body { height: 100%; margin: 0; padding: 0; background: #f2f2f2; font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', '맑은 고딕', '돋움', Dotum, sans-serif; }
    .dc-wrapper { width: 100%; height: 100vh; display: flex; flex-direction: column; background: #fff; }
    .dc-header { background: #534884; color: white; padding: 8px 12px; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
    .dc-header h1 { margin: 0; font-size: 15px; cursor: pointer; color: #ffffff; }
    .dc-header-right { display: flex; align-items: center; gap: 8px; }
    .dc-clock { font-size: 12px; background: #3C345E; color: #2ee6b5; padding: 3px 9px; border-radius: 3px; font-weight: bold; }
    .dc-header span.gal-tag { font-size: 10px; background: #3C345E; padding: 2px 6px; border-radius: 3px; }
    .nav-tabs { display: flex; background: #3C345E; flex-shrink: 0; overflow-x: auto; }
    .nav-tab { flex: 1; text-align: center; padding: 8px 4px; color: #ccc; font-size: 11px; font-weight: bold; cursor: pointer; white-space: nowrap; min-width: 55px; }
    .nav-tab.active { background: #fff; color: #534884; }
    .nav-tab.lock::after { content: ' 🔒'; font-size: 9px; }
    .dc-main-layout { display: flex; flex: 1; overflow: hidden; }
    .dc-sponsor-col { width: 220px; background: #f8f9fa; padding: 8px; flex-shrink: 0; border-right: 1px solid #d1d1d1; display: flex; flex-direction: column; gap: 8px; overflow-y: auto; }
    .ad-banner-box { border: 1px dashed #94a3b8; border-radius: 3px; display: flex; align-items: center; justify-content: center; text-align: center; font-size: 10px; color: #94a3b8; background: #f1f5f9; line-height: 1.5; overflow: hidden; }
    .ad-banner-box img { width: 100%; height: 100%; object-fit: cover; display: block; }
    /* 📌 화면이 브라우저 창 전체 높이를 채우게 되면서 광고란 두 개만으로는 옆 칸이
       다 안 채워져 아래에 빈 여백이 크게 남았습니다. flex로 남는 세로 공간을
       두 광고란이 2:1 비율로 나눠 채우도록 해서 여백이 남지 않게 합니다. */
    .ad-banner-box-1 { min-height: 300px; flex: 2 1 auto; }
    .ad-banner-box-2 { min-height: 140px; flex: 1 1 auto; }
    .dc-content { flex: 1; overflow-y: auto; padding: 10px; border-right: 1px solid #ddd; }
    .dc-sidebar { width: 210px; background: #f8f9fa; padding: 8px; flex-shrink: 0; border-left: 1px solid #d1d1d1; display: flex; flex-direction: column; gap: 8px; overflow-y: auto; }
    .dc-box { border: 1px solid #d1d1d1; background: #fff; padding: 8px; }
    .dc-title { font-size: 11px; font-weight: bold; color: #534884; margin-bottom: 6px; border-bottom: 1px solid #534884; padding-bottom: 3px; display: flex; justify-content: space-between; align-items: center; }
    .input-row { display: flex; gap: 4px; margin-bottom: 4px; }
    input, textarea, select { padding: 4px 6px; border: 1px solid #ccc; font-size: 11px; font-family: inherit; }
    input.auth-input { width: 50%; }
    input.full-input { width: 100%; margin-bottom: 4px; }
    textarea { width: 100%; height: 48px; resize: none; margin-bottom: 4px; line-height: 1.4; }
    .dc-btn { padding: 4px 8px; background: #534884; color: white; border: none; font-size: 11px; font-weight: bold; cursor: pointer; border-radius: 2px; }
    .dc-btn:disabled { background: #888 !important; cursor: not-allowed; }
    .dc-btn-write { background: #534884; width: 100%; padding: 6px; }
    .dc-btn-open-write { background: #534884; font-size: 11px; padding: 5px 12px; white-space: nowrap; }
    .dc-btn-danger { background: #e74c3c; }
    .dc-btn-delete { background: #555; }
    .btn-compact { font-size: 10px; padding: 2px 6px; }
    .post-action-btn { font-size: 11px; padding: 4px 10px; margin-left: 4px; }
    .dc-btn-admin-req { background: #16a085; width: 100%; font-size: 11px; padding: 6px; font-weight: bold; }
    .hidden { display: none !important; }
    /* 📌 텍스트 규격 및 테이블 최적화 */
    .dc-table { width: 100%; border-collapse: collapse; font-size: 11px; text-align: center; table-layout: fixed; }
    .dc-table th { background: #f2f2f2; border-top: 1px solid #534884; border-bottom: 1px solid #ddd; padding: 6px 2px; }
    .dc-table td { border-bottom: 1px solid #eee; padding: 6px 2px; color: #444; }
    .dc-table .title-td { text-align: left; padding-left: 4px; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; word-break: break-all; }
    .badge-gal { background: #7f8c8d; color: white; font-size: 9px; padding: 1px 3px; border-radius: 2px; margin-right: 2px; font-weight: normal; }
    .badge-concept { background: #ff6b6b; color: white; font-size: 9px; padding: 1px 3px; border-radius: 2px; margin-right: 2px; font-weight: normal; }
    .badge-admin { background: #8e44ad; color: white; font-size: 9px; padding: 1px 3px; border-radius: 2px; margin-right: 2px; font-weight: normal; }
    .comment-count { color: #e64c3c; font-size: 10px; font-weight: bold; }
    .user-id { color: #888; font-size: 10px; }
    .home-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .home-card { border: 1px solid #cbd5e1; background: #fff; padding: 6px; border-radius: 3px; }
    .home-card-full { grid-column: 1 / -1; }
    .home-card-header { font-size: 11px; font-weight: bold; color: #534884; border-bottom: 1.5px solid #534884; padding-bottom: 3px; margin-bottom: 4px; display: flex; justify-content: space-between; }
    .home-list { list-style: none; padding: 0; margin: 0; font-size: 11px; }
    .home-list li { padding: 4px 0; border-bottom: 1px solid #f1f5f9; display: flex; justify-content: space-between; cursor: pointer; word-break: break-all; }
    /* 📌 게시글 및 댓글 텍스트 줄바꿈 / 자간 / 정렬 규격 개선 */
    .post-view { border: 1px solid #534884; background: #fff; padding: 10px; margin-bottom: 8px; }
    .post-view-header { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px dashed #ccc; padding-bottom: 6px; }
    .post-view-title { font-size: 13px; font-weight: bold; color: #534884; word-break: keep-all; word-wrap: break-word; line-height: 1.4; }
    .post-view-meta { font-size: 10px; color: #888; margin: 4px 0 8px 0; }
    .post-view-content { font-size: 12px; line-height: 1.6; color: #222; min-height: 50px; white-space: pre-wrap; word-break: keep-all; word-wrap: break-word; overflow-wrap: break-word; margin-bottom: 10px; }
    .post-img { max-width: 100%; max-height: 260px; display: block; margin: 8px 0; border: 1px solid #ddd; }
    .vote-box { display: flex; justify-content: center; gap: 8px; margin: 10px 0; }
    .btn-vote { display: flex; flex-direction: column; align-items: center; justify-content: center; width: 52px; height: 38px; border: 1px solid #ccc; background: #fdfdfd; cursor: pointer; border-radius: 4px; font-size: 10px; }
    .btn-vote.up { border-color: #534884; color: #534884; }
    .btn-vote:disabled { opacity: 0.5; cursor: not-allowed; }
    /* 📌 댓글 영역 텍스트 및 간격 핏 조율 */
    .comment-section { border-top: 1px solid #534884; padding-top: 6px; background: #fafafa; padding: 8px; }
    .comment-list { list-style: none; padding: 0; margin: 0 0 8px 0; }
    .comment-item { border-bottom: 1px solid #e5e7eb; padding: 5px 0; font-size: 11px; display: flex; justify-content: space-between; line-height: 1.4; }
    .comment-body { word-break: keep-all; word-wrap: break-word; overflow-wrap: break-word; white-space: pre-wrap; margin-top: 2px; color: #333; }
    .comment-img { max-width: 100%; max-height: 120px; display: block; margin-top: 4px; border: 1px solid #ddd; }
    .toolbar-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; gap: 6px; }
    .search-box { display: flex; gap: 2px; flex: 1; }
    .search-input { width: 100%; padding: 3px 6px; font-size: 10px; }
    .sort-select { padding: 2px 4px; font-size: 10px; border: 1px solid #534884; background: #fff; color: #534884; font-weight: bold; }
    .status-badge { font-size: 10px; padding: 3px 6px; background: #eef2ff; color: #534884; border: 1px solid #534884; border-radius: 3px; margin-bottom: 6px; text-align: center; }
    .notice-box-text { font-size: 11px; color: #555; line-height: 1.5; }
    /* 모달 모듈 (관리자 요청) */
    .modal-backdrop { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .modal-box { background: #fff; border: 2px solid #534884; width: 320px; padding: 12px; border-radius: 4px; box-shadow: 0 4px 10px rgba(0,0,0,0.15); }
    #intro-overlay { position: fixed; top:0; left:0; width:100%; height:100%; background:#0b0714; z-index: 99999; display:flex; align-items:center; justify-content:center; transition: opacity 0.6s ease; }
    #intro-overlay.hide { opacity: 0; pointer-events: none; }
    #intro-overlay video { max-width: 100%; max-height: 100%; }
    #intro-skip { position: absolute; bottom: 24px; right: 24px; color: #fff; background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.4); padding: 6px 14px; font-size: 12px; border-radius: 20px; cursor: pointer; }
  </style>
</head>
<body>
<div id="intro-overlay">
  <video id="intro-video" autoplay muted playsinline src="data:video/mp4;base64,{{INTRO_VIDEO_B64}}"></video>
  <div id="intro-skip" onclick="skipIntro()">건너뛰기 ▶</div>
</div>
<div class="dc-wrapper">
  <div class="dc-header">
    <h1 onclick="switchGallery('home')" style="display:flex; align-items:center; gap:6px;"><img src="data:image/png;base64,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" alt="온리새롬 로고" style="height:22px; width:auto; border-radius:3px; vertical-align:middle;">온리새롬 갤러리</h1>
    <div class="dc-header-right">
      <div id="realtime-clock" class="dc-clock">로딩 중...</div>
      <span id="current-gallery-title" class="gal-tag">🏠</span>
    </div>
  </div>
  <div id="nav-bar" class="nav-tabs">
    <div id="tab-home" class="nav-tab active" onclick="switchGallery('home')">🏠 홈</div>
    <div id="tab-all" class="nav-tab" onclick="switchGallery('all')">🌐 전체</div>
    <div id="tab-g1" class="nav-tab lock" onclick="switchGallery('g1')">🥇 1학년</div>
    <div id="tab-g2" class="nav-tab lock" onclick="switchGallery('g2')">🥈 2학년</div>
    <div id="tab-g3" class="nav-tab lock" onclick="switchGallery('g3')">🥉 3학년</div>
    <div id="tab-dating" class="nav-tab" onclick="switchGallery('dating')">💕 여소남소</div>
    <div id="tab-study" class="nav-tab" onclick="switchGallery('study')">📚 학습</div>
    <div id="tab-overseas_fb" class="nav-tab" onclick="switchGallery('overseas_fb')">⚽ 해외축구</div>
    <div id="tab-admin_notice" class="nav-tab" onclick="switchGallery('admin_notice')">📢 관리자</div>
    <div id="tab-concept" class="nav-tab" onclick="switchGallery('concept')">🔥 인기글</div>
    <div id="tab-saved" class="nav-tab" onclick="switchGallery('saved')">⭐ 저장됨</div>
  </div>
  <div class="dc-main-layout">
    <!-- 좌측 광고판 컬럼 -->
    <div class="dc-sponsor-col">
      <div class="dc-title" style="margin-bottom:2px;">📣 광고란 1</div>
      <div id="ad-banner-box-1" class="ad-banner-box ad-banner-box-1">
        <div id="ad-banner-placeholder-1">광고 이미지 없음<br>광고 관련 문의는 관리자에게</div>
        <img id="ad-banner-img-1" class="hidden">
      </div>
      <!-- 📌 관리자 전용 광고란 1 이미지 등록 -->
      <div id="ad-admin-upload-1" class="hidden">
        <input type="file" id="ad-file-1" accept="image/*" style="font-size:9px; width:100%; margin-bottom:4px;">
        <button class="dc-btn" style="width:100%; background:#534884;" onclick="uploadAdBanner(1)">광고란 1에 등록</button>
      </div>
      <div class="dc-title" style="margin-bottom:2px; margin-top:6px;">📣 광고란 2</div>
      <div id="ad-banner-box-2" class="ad-banner-box ad-banner-box-2">
        <div id="ad-banner-placeholder-2">광고 이미지 없음<br>광고 관련 문의는 관리자에게</div>
        <img id="ad-banner-img-2" class="hidden">
      </div>
      <!-- 📌 관리자 전용 광고란 2 이미지 등록 -->
      <div id="ad-admin-upload-2" class="hidden">
        <input type="file" id="ad-file-2" accept="image/*" style="font-size:9px; width:100%; margin-bottom:4px;">
        <button class="dc-btn" style="width:100%; background:#534884;" onclick="uploadAdBanner(2)">광고란 2에 등록</button>
      </div>
    </div>
    <div class="dc-content">
      <!-- 상세글 보기 -->
      <div id="post-view-box" class="post-view hidden">
        <div class="post-view-header">
          <div id="view-title" class="post-view-title"></div>
          <div style="flex-shrink:0;">
            <button id="btn-edit" class="dc-btn post-action-btn" style="background:#f0ad4e; display:none;" onclick="openEditBox()">✏️ 수정</button>
            <button id="btn-save" class="dc-btn post-action-btn" style="background:#95a5a6;" onclick="toggleSavePost()">☆ 저장</button>
            <button id="btn-delete" class="dc-btn post-action-btn dc-btn-delete" style="display:none;" onclick="deleteCurrentPost()">🗑️ 삭제</button>
            <button class="dc-btn post-action-btn dc-btn-danger" onclick="reportCurrentPost()">🚨 신고</button>
          </div>
        </div>
        <div id="view-meta" class="post-view-meta"></div>
        <div id="view-content" class="post-view-content"></div>
        <div id="view-image-container"></div>
        <div class="vote-box">
          <button class="btn-vote up" onclick="vote('up')">👍 추천 <span id="up-count">0</span></button>
          <button class="btn-vote down" onclick="vote('down')">👎 비추 <span id="down-count">0</span></button>
        </div>
        <div class="comment-section">
          <div style="font-size:11px; font-weight:bold; color:#534884; margin-bottom:4px;">💬 댓글</div>
          <div id="comment-list" class="comment-list"></div>
          <div id="comment-login-notice" class="status-badge hidden">🔒 댓글 작성은 로그인 후 이용 가능합니다.</div>
          <div id="comment-write-area">
            <input type="text" id="cmt-author" class="full-input" placeholder="닉네임" value="ㅇㅇ">
            <textarea id="cmt-content" placeholder="댓글 내용을 입력하세요..."></textarea>
            <div style="display:flex; align-items:center; gap:2px; margin-bottom:4px;">
              <input type="file" id="cmt-file" accept="image/*" style="font-size:9px; width:100%;">
            </div>
            <button id="btn-submit-cmt" class="dc-btn" style="width: 100%;" onclick="submitComment()">댓글 등록</button>
          </div>
        </div>
        <button class="dc-btn" style="margin-top: 6px; width:100%; background:#666;" onclick="closeView()">목록으로 돌아가기</button>
      </div>
      <!-- 홈 화면 -->
      <div id="view-home">
        <div class="home-grid">
          <div class="home-card">
            <div class="home-card-header">
              <span>🔥 인기글</span>
              <span style="font-size:9px; cursor:pointer;" onclick="switchGallery('concept')">더보기+</span>
            </div>
            <ul id="home-concept-list" class="home-list"></ul>
          </div>
          <div class="home-card">
            <div class="home-card-header">
              <span>💕 여소남소 갤러리</span>
              <span style="font-size:9px; cursor:pointer;" onclick="switchGallery('dating')">더보기+</span>
            </div>
            <ul id="home-dating-list" class="home-list"></ul>
          </div>
          <div class="home-card">
            <div class="home-card-header">
              <span>📚 학습 갤러리</span>
              <span style="font-size:9px; cursor:pointer;" onclick="switchGallery('study')">더보기+</span>
            </div>
            <ul id="home-study-list" class="home-list"></ul>
          </div>
          <div class="home-card">
            <div class="home-card-header">
              <span>⚽ 해외축구 갤러리</span>
              <span style="font-size:9px; cursor:pointer;" onclick="switchGallery('overseas_fb')">더보기+</span>
            </div>
            <ul id="home-overseas-list" class="home-list"></ul>
          </div>
          <div class="home-card home-card-full">
            <div class="home-card-header">
              <span>🌐 전체 최신글</span>
              <span style="font-size:9px; cursor:pointer;" onclick="switchGallery('all')">더보기+</span>
            </div>
            <ul id="home-recent-list" class="home-list"></ul>
          </div>
        </div>
      </div>
      <!-- 게시판 목록 화면 -->
      <div id="view-list" class="hidden">
        <div class="toolbar-container" style="margin-top: 6px;">
          <div class="search-box">
            <input type="text" id="search-kw" class="search-input" placeholder="검색어 입력 (제목/내용)" onkeyup="if(event.key==='Enter') executeSearch()">
            <button class="dc-btn" onclick="executeSearch()">검색</button>
          </div>
          <select id="sort-select" class="sort-select" onchange="changeSort(this.value)">
            <option value="date">📅 최신순</option>
            <option value="upvotes">🔥 인기순</option>
            <option value="comments">💬 댓글많은순</option>
            <option value="views">👀 조회순</option>
          </select>
          <button id="btn-open-write" class="dc-btn dc-btn-open-write" onclick="openWriteBox()">✍️ 글쓰기</button>
        </div>
        <table class="dc-table">
          <thead>
            <tr>
              <th style="width: 12%;">번호</th>
              <th style="width: 50%;">제목</th>
              <th style="width: 24%;">작성자</th>
              <th style="width: 14%;">추천</th>
            </tr>
          </thead>
          <tbody id="post-list"></tbody>
        </table>
      </div>
      <!-- 글쓰기 화면 (글쓰기 버튼을 눌러야만 별도 화면으로 전환됨) -->
      <div id="view-write" class="hidden">
        <div id="write-box" class="dc-box">
          <div class="dc-title">
            <span id="write-box-title">✍️ 글쓰기</span>
            <span id="my-id-display" style="font-size:9px; font-weight:normal;"></span>
          </div>
          <div id="write-form-container">
            <input type="text" id="post-author" class="full-input" placeholder="닉네임" value="ㅇㅇ">
            <input type="text" id="post-title" class="full-input" placeholder="제목">
            <textarea id="post-content" placeholder="내용을 입력하세요..." style="height:140px;"></textarea>
            <div style="display:flex; align-items:center; gap:2px; margin-bottom:4px;">
              <span style="font-size:10px; color:#555;">📷 사진:</span>
              <input type="file" id="post-file" accept="image/*" style="font-size:9px; width:100%;">
            </div>
            <div style="display:flex; gap:4px;">
              <button id="btn-submit-post" class="dc-btn dc-btn-write" style="flex:1;" onclick="submitPost()">등록</button>
              <button class="dc-btn" style="flex-shrink:0; background:#7f8c8d;" onclick="closeWriteBox()">취소</button>
            </div>
          </div>
        </div>
        <button class="dc-btn" style="margin-top: 6px; width:100%; background:#666;" onclick="closeWriteBox()">목록으로 돌아가기</button>
      </div>
    </div>
    <!-- 사이드바 -->
    <div class="dc-sidebar">
      <div id="auth-box-unauth" class="dc-box">
        <div class="dc-title">🔑 로그인</div>
        <div style="font-size:9px; color:#666; margin-bottom:6px; line-height:1.3;">
          비로그인 상태에서는 글 조회만 가능합니다.<br>글쓰기/댓글은 로그인이 필요합니다.
        </div>
        <input type="text" class="full-input" id="login-id" placeholder="학번 (예: 2610101)">
        <input type="password" class="full-input" id="login-pw" placeholder="비밀번호">
        <button class="dc-btn" style="width:100%; margin-bottom:4px;" onclick="loginAccount()">로그인</button>
        <div id="login-msg" style="font-size:9px; color:#e74c3c; margin-bottom:6px; word-break:break-all;"></div>
        <div style="text-align:center; font-size:9px; color:#888; margin-bottom:4px; border-top:1px dashed #ccc; padding-top:6px;">
          계정이 없으신가요?
        </div>
        <button class="dc-btn" style="width:100%; background:#7f8c8d;" onclick="toggleSignupBox()">✉️ 학번 메일로 계정 만들기</button>
        <div id="signup-box" class="hidden" style="margin-top:8px; border-top:1px dashed #ccc; padding-top:8px;">
          <div style="font-size:9px; color:#666; margin-bottom:6px; line-height:1.3;">
            학번 메일로 인증 후 비밀번호를 설정하면 계정이 생성되어, 다음부터는 이메일 인증 없이 로그인만으로 이용할 수 있습니다.<br>
            • 26... : 1학년<br>• 25... : 2학년<br>• 24... : 3학년
          </div>
          <input type="email" class="full-input" id="email" placeholder="학번메일 (예: 2610101@saerom.hs.kr)">
          <button class="dc-btn" style="width:100%; margin-bottom:4px;" onclick="sendEmailCode()">인증번호 메일 발송</button>
          <div id="code-step" class="hidden" style="margin-top: 4px;">
            <input type="text" class="full-input" id="auth-code" placeholder="인증번호 6자리">
            <button class="dc-btn" style="width:100%; background:#27ae60;" onclick="verifyCode()">인증 완료</button>
          </div>
          <div id="pw-set-step" class="hidden" style="margin-top: 4px;">
            <input type="password" class="full-input" id="new-pw" placeholder="사용할 비밀번호 (4자 이상)">
            <input type="password" class="full-input" id="new-pw-confirm" placeholder="비밀번호 확인">
            <button class="dc-btn" style="width:100%; background:#27ae60;" onclick="setAccountPassword()">계정 생성 완료</button>
          </div>
          <div id="auth-msg" style="font-size:9px; color:#e74c3c; margin-top:4px; word-break:break-all;"></div>
        </div>
      </div>
      <div id="auth-box-auth" class="dc-box hidden">
        <div class="dc-title">👤 내 학생 정보</div>
        <div class="status-badge" id="user-status-text">인증됨</div>
        <button class="dc-btn" style="width:100%; background:#e74c3c; margin-bottom:6px;" onclick="logout()">로그아웃/재인증</button>
        <button class="dc-btn" style="width:100%; background:#7f8c8d;" onclick="toggleChangePwBox()">🔑 비밀번호 변경</button>
        <div id="change-pw-box" class="hidden" style="margin-top:8px; border-top:1px dashed #ccc; padding-top:8px;">
          <input type="password" class="full-input" id="cur-pw" placeholder="현재 비밀번호">
          <input type="password" class="full-input" id="new-pw2" placeholder="새 비밀번호 (4자 이상)">
          <input type="password" class="full-input" id="new-pw2-confirm" placeholder="새 비밀번호 확인">
          <button class="dc-btn" style="width:100%; background:#27ae60;" onclick="changePassword()">변경하기</button>
          <div id="change-pw-msg" style="font-size:9px; color:#e74c3c; margin-top:4px; word-break:break-all;"></div>
        </div>
      </div>
      <!-- 📌 관리자에게 요청 버튼 -->
      <div class="dc-box" style="border-color:#16a085;">
        <div class="dc-title" style="color:#16a085; border-color:#16a085;">✉️ 관리자 센터</div>
        <button class="dc-btn dc-btn-admin-req" onclick="openAdminReqModal()">관리자에게 요청하기</button>
      </div>
      <!-- 📌 관리자 공지사항 -->
      <div class="dc-box" style="border-color:#f39c12;">
        <div class="dc-title" style="color:#f39c12; border-color:#f39c12;">📢 관리자 공지사항</div>
        <div class="notice-box-text">교칙에 위배되는 글 및 사진 게시 시 학번 이름 공개 및 제재를 할 것입니다.</div>
      </div>
    </div>
  </div>
</div>
<!-- 📌 관리자 요청 모달 -->
<div id="admin-req-modal" class="modal-backdrop hidden">
  <div class="modal-box">
    <div class="dc-title">✉️ 관리자 문의 및 요청</div>
    <div style="margin-bottom:6px;">
      <label style="font-size:10px; font-weight:bold; color:#555;">요청 유형</label>
      <select id="req-category" class="full-input" style="margin-top:2px;">
        <option value="웹사이트 수정 요청">🌐 웹사이트 수정 요청</option>
        <option value="홍보 및 제휴 문의">📢 홍보 및 제휴 문의</option>
        <option value="기능 추가 건의">💡 기능 추가 건의</option>
        <option value="기타 문의">💬 기타 문의</option>
      </select>
    </div>
    <div style="margin-bottom:6px;">
      <label style="font-size:10px; font-weight:bold; color:#555;">연락처/학번 (선택)</label>
      <input type="text" id="req-contact" class="full-input" placeholder="답변받을 이메일 또는 이름/학번">
    </div>
    <div style="margin-bottom:6px;">
      <label style="font-size:10px; font-weight:bold; color:#555;">상세 내용</label>
      <textarea id="req-content" style="height:70px;" placeholder="관리자에게 전송할 내용을 상세히 작성해주세요."></textarea>
    </div>
    <div style="display:flex; gap:4px;">
      <button class="dc-btn" style="flex:1; background:#16a085;" onclick="submitAdminReq()">전송하기</button>
      <button class="dc-btn" style="flex:1; background:#7f8c8d;" onclick="closeAdminReqModal()">취소</button>
    </div>
  </div>
</div>
<script>
  // 📌 이 앱은 더 이상 Google Colab 안에서 돌지 않지만, 기존에 검증된 프론트엔드 코드를
  //    그대로 재사용하기 위해 google.colab.kernel.invokeFunction(...) 호출을 실제
  //    백엔드(Flask, /api/*)로 연결해주는 얇은 호환 계층만 추가했습니다.
  const google = {
    colab: {
      kernel: {
        invokeFunction: function(name, args) {
          const fn = name.replace('notebook.', '');
          return fetch('/api/' + fn, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(args)
          })
            .then(r => r.json())
            .then(data => ({ data: { 'application/json': data } }))
            .catch(err => ({ data: { 'application/json': { success: false, msg: '네트워크 오류: ' + err.message } } }));
        }
      }
    }
  };
  let currentGallery = 'home', currentSort = 'date', currentPostId = null, currentSearchKw = '';
  let serverAuthCode = "", userGrade = null, serverIsAdmin = false;
  let currentPostData = null, editingPostId = null;
  const galNames = {
    'home': '홈', 'all': '전체 갤러리', 'g1': '1학년 갤러리', 'g2': '2학년 갤러리',
    'g3': '3학년 갤러리', 'dating': '여소/남소 갤러리', 'study': '학습 갤러리',
    'overseas_fb': '해외축구 갤러리', 'admin_notice': '📢 관리자 채널', 'concept': '인기글',
    'saved': '⭐ 저장한 글'
  };
  function startClock() {
    const weekdays = ['일', '월', '화', '수', '목', '금', '토'];
    function update() {
      const now = new Date();
      const yyyy = now.getFullYear();
      const mm = String(now.getMonth() + 1).padStart(2, '0');
      const dd = String(now.getDate()).padStart(2, '0');
      const hh = String(now.getHours()).padStart(2, '0');
      const mi = String(now.getMinutes()).padStart(2, '0');
      const ss = String(now.getSeconds()).padStart(2, '0');
      const day = weekdays[now.getDay()];
      document.getElementById('realtime-clock').innerText = `${yyyy}.${mm}.${dd}(${day}) ${hh}:${mi}:${ss}`;
    }
    update();
    setInterval(update, 1000);
  }
  function isLoggedIn() {
    return !!localStorage.getItem('saerom_student_id');
  }
  function getMyUserId() {
    const sid = localStorage.getItem('saerom_student_id');
    if (sid) return sid;
    let uid = localStorage.getItem('saerom_user_num');
    if (!uid) {
      uid = Math.floor(100 + Math.random() * 900).toString();
      localStorage.setItem('saerom_user_num', uid);
    }
    return uid;
  }
  function parseRes(obj) {
    try {
      if (!obj || !obj.data || obj.data['application/json'] === undefined) return { success: false, msg: "오류 발생" };
      let data = obj.data['application/json'];
      return typeof data === 'string' ? JSON.parse(data) : data;
    } catch (e) { return { success: false, msg: e.message }; }
  }
  function sendEmailCode() {
    const email = document.getElementById('email').value.trim();
    const msgDiv = document.getElementById('auth-msg');
    msgDiv.innerText = "메일 발송 중...";
    google.colab.kernel.invokeFunction('notebook.send_email', [email], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        serverAuthCode = res.code;
        userGrade = res.grade;
        serverIsAdmin = !!res.is_admin;
        msgDiv.style.color = "#27ae60";
        msgDiv.innerText = res.msg;
        document.getElementById('code-step').classList.remove('hidden');
      } else {
        msgDiv.style.color = "#e74c3c";
        msgDiv.innerText = res.msg;
      }
    });
  }
  function verifyCode() {
    const inputCode = document.getElementById('auth-code').value.trim();
    if (inputCode === serverAuthCode && userGrade) {
      document.getElementById('pw-set-step').classList.remove('hidden');
    } else {
      alert("인증번호가 일치하지 않습니다.");
    }
  }
  function toggleSignupBox() {
    document.getElementById('signup-box').classList.toggle('hidden');
  }
  // 📌 이메일 인증 완료 후 비밀번호를 설정해 계정을 생성 (다음부터는 이메일 인증 없이 로그인만으로 이용 가능)
  function setAccountPassword() {
    const email = document.getElementById('email').value.trim();
    const pw1 = document.getElementById('new-pw').value;
    const pw2 = document.getElementById('new-pw-confirm').value;
    const msgDiv = document.getElementById('auth-msg');
    if (!pw1 || pw1.length < 4) { alert("비밀번호는 4자 이상 입력해주세요."); return; }
    if (pw1 !== pw2) { alert("비밀번호가 일치하지 않습니다."); return; }
    google.colab.kernel.invokeFunction('notebook.create_account', [email, pw1], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        const studentId = email.split('@')[0];
        localStorage.setItem('saerom_verified_grade', res.grade);
        localStorage.setItem('saerom_is_admin', res.is_admin ? 'true' : 'false');
        localStorage.setItem('saerom_student_id', studentId);
        applyGradeUI(res.grade, res.is_admin);
        alert(`계정이 생성되어 로그인되었습니다!${res.is_admin ? ' [👑 관리자 권한 부여됨]' : ''}`);
      } else {
        msgDiv.style.color = "#e74c3c";
        msgDiv.innerText = res.msg;
      }
    });
  }
  // 📌 학번+비밀번호 로그인 (계정이 있으면 매번 이메일 인증할 필요 없음)
  function loginAccount() {
    const id = document.getElementById('login-id').value.trim();
    const pw = document.getElementById('login-pw').value;
    const msgDiv = document.getElementById('login-msg');
    if (!id || !pw) { msgDiv.innerText = "학번과 비밀번호를 입력하세요."; return; }
    google.colab.kernel.invokeFunction('notebook.login', [id, pw], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        localStorage.setItem('saerom_verified_grade', res.grade);
        localStorage.setItem('saerom_is_admin', res.is_admin ? 'true' : 'false');
        localStorage.setItem('saerom_student_id', id);
        applyGradeUI(res.grade, res.is_admin);
      } else {
        msgDiv.innerText = res.msg;
      }
    });
  }
  function toggleChangePwBox() {
    document.getElementById('change-pw-box').classList.toggle('hidden');
  }
  function changePassword() {
    const curPw = document.getElementById('cur-pw').value;
    const newPw = document.getElementById('new-pw2').value;
    const newPwConfirm = document.getElementById('new-pw2-confirm').value;
    const msgDiv = document.getElementById('change-pw-msg');
    if (!curPw || !newPw) { msgDiv.innerText = "모든 항목을 입력하세요."; return; }
    if (newPw.length < 4) { msgDiv.innerText = "새 비밀번호는 4자 이상 입력해주세요."; return; }
    if (newPw !== newPwConfirm) { msgDiv.innerText = "새 비밀번호가 일치하지 않습니다."; return; }
    const sid = localStorage.getItem('saerom_student_id');
    google.colab.kernel.invokeFunction('notebook.change_password', [sid, curPw, newPw], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        alert(res.msg);
        document.getElementById('cur-pw').value = '';
        document.getElementById('new-pw2').value = '';
        document.getElementById('new-pw2-confirm').value = '';
        msgDiv.innerText = '';
        document.getElementById('change-pw-box').classList.add('hidden');
      } else {
        msgDiv.innerText = res.msg;
      }
    });
  }
  function applyGradeUI(grade, isAdmin) {
    document.getElementById('auth-box-unauth').classList.add('hidden');
    document.getElementById('auth-box-auth').classList.remove('hidden');
    const roleText = isAdmin ? '👑 관리자' : `${grade}학년 학생`;
    document.getElementById('user-status-text').innerText = `로그인됨: ${roleText}`;
    const targetTab = document.getElementById('tab-g' + grade);
    if (targetTab) targetTab.classList.remove('lock');
    if (isAdmin) {
      ['1', '2', '3'].forEach(g => {
        const t = document.getElementById('tab-g' + g);
        if (t) t.classList.remove('lock');
      });
    }
    setAdAdminUploadVisible(isAdmin);
    if (currentGallery !== 'home') switchGallery(currentGallery);
  }
  // 📌 location.reload()는 Colab 출력 iframe에서 흰 화면으로 빠지는 문제가 있어,
  //    새로고침 없이 로그아웃 상태로 화면을 직접 초기화합니다.
  function logout() {
    localStorage.removeItem('saerom_verified_grade');
    localStorage.removeItem('saerom_is_admin');
    localStorage.removeItem('saerom_student_id');
    userGrade = null;
    serverIsAdmin = false;
    serverAuthCode = "";
    closeView();
    closeWriteBox();
    document.getElementById('auth-box-auth').classList.add('hidden');
    document.getElementById('auth-box-unauth').classList.remove('hidden');
    document.getElementById('signup-box').classList.add('hidden');
    document.getElementById('code-step').classList.add('hidden');
    document.getElementById('pw-set-step').classList.add('hidden');
    document.getElementById('login-id').value = '';
    document.getElementById('login-pw').value = '';
    document.getElementById('login-msg').innerText = '';
    document.getElementById('email').value = '';
    document.getElementById('auth-code').value = '';
    document.getElementById('new-pw').value = '';
    document.getElementById('new-pw-confirm').value = '';
    document.getElementById('auth-msg').innerText = '';
    ['1', '2', '3'].forEach(g => {
      const tab = document.getElementById('tab-g' + g);
      if (tab) tab.classList.add('lock');
    });
    setAdAdminUploadVisible(false);
    const targetGallery = ['g1', 'g2', 'g3'].includes(currentGallery) ? 'home' : currentGallery;
    switchGallery(targetGallery);
  }
  function openAdminReqModal() {
    document.getElementById('admin-req-modal').classList.remove('hidden');
  }
  function closeAdminReqModal() {
    document.getElementById('admin-req-modal').classList.add('hidden');
  }
  function submitAdminReq() {
    const cat = document.getElementById('req-category').value;
    const contact = document.getElementById('req-contact').value.trim();
    const content = document.getElementById('req-content').value.trim();
    if (!content) {
      alert("요청 내용을 입력해주세요.");
      return;
    }
    google.colab.kernel.invokeFunction('notebook.send_admin_request', [cat, contact, content], {}).then(obj => {
      const res = parseRes(obj);
      alert(res.msg);
      if (res.success) {
        document.getElementById('req-content').value = '';
        document.getElementById('req-contact').value = '';
        closeAdminReqModal();
      }
    });
  }
  // 📌 글쓰기 화면 전환 (DC 갤러리처럼 '글쓰기' 버튼을 눌러야만 별도 작성 화면으로 넘어감)
  function openWriteBox() {
    if (!isLoggedIn()) {
      alert("글쓰기는 로그인 후 이용 가능합니다. 사이드바에서 로그인하거나 계정을 먼저 만들어주세요.");
      return;
    }
    editingPostId = null;
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    document.getElementById('write-box-title').innerText = (currentGallery === 'admin_notice')
      ? (isAdmin ? "✍️ 관리자 공지 작성" : "🔒 관리자 채널 (관리자만 작성 가능)")
      : "✍️ 글쓰기";
    document.getElementById('btn-submit-post').innerText = "등록";
    document.getElementById('post-title').value = '';
    document.getElementById('post-content').value = '';
    document.getElementById('post-file').value = '';
    document.getElementById('view-list').classList.add('hidden');
    document.getElementById('post-view-box').classList.add('hidden');
    document.getElementById('view-write').classList.remove('hidden');
  }
  function closeWriteBox() {
    const wasEditing = !!editingPostId;
    editingPostId = null;
    document.getElementById('view-write').classList.add('hidden');
    document.getElementById('write-box-title').innerText = "✍️ 글쓰기";
    document.getElementById('btn-submit-post').innerText = "등록";
    document.getElementById('view-list').classList.remove('hidden');
    if (wasEditing) document.getElementById('post-view-box').classList.remove('hidden');
  }
  // 📌 게시글 수정 화면 열기 (작성자 본인 또는 관리자만 버튼이 노출됨)
  function openEditBox() {
    if (!currentPostData || !isLoggedIn()) return;
    editingPostId = currentPostId;
    document.getElementById('post-title').value = currentPostData.title;
    document.getElementById('post-content').value = currentPostData.content;
    document.getElementById('write-box-title').innerText = "✏️ 글 수정";
    document.getElementById('btn-submit-post').innerText = "수정 완료";
    document.getElementById('view-list').classList.add('hidden');
    document.getElementById('post-view-box').classList.add('hidden');
    document.getElementById('view-write').classList.remove('hidden');
  }
  // 📌 갤러리 하나에 맞는 "목록 화면" UI(글쓰기 버튼, 검색/정렬 노출 여부, 목록 데이터)를
  //    구성합니다. 상단 탭을 눌러 갤러리를 바꿀 때(switchGallery)와, 홈/인기글 카드에서
  //    다른 갤러리의 글을 곧바로 열었을 때(syncGalleryContextForPost) 공용으로 사용합니다.
  function applyGalleryListUI(gal) {
    const vHome = document.getElementById('view-home');
    const vList = document.getElementById('view-list');
    const writeFormContainer = document.getElementById('write-form-container');
    const btnOpenWrite = document.getElementById('btn-open-write');
    const searchBox = document.querySelector('.search-box');
    const sortSelect = document.getElementById('sort-select');
    vHome.classList.add('hidden');
    vList.classList.remove('hidden');
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    if (gal === 'saved') {
      // 📌 저장한 글 목록: 글쓰기/검색/정렬 없이 저장한 글만 보여줌
      btnOpenWrite.classList.add('hidden');
      if (searchBox) searchBox.classList.add('hidden');
      if (sortSelect) sortSelect.classList.add('hidden');
      loadSavedPosts();
      return;
    }
    if (searchBox) searchBox.classList.remove('hidden');
    if (sortSelect) sortSelect.classList.remove('hidden');
    if (gal === 'concept') {
      btnOpenWrite.classList.add('hidden');
    } else if (gal === 'admin_notice') {
      btnOpenWrite.classList.remove('hidden');
      if (isAdmin) {
        writeFormContainer.classList.remove('hidden');
        document.getElementById('write-box-title').innerText = "✍️ 관리자 공지 작성";
      } else {
        writeFormContainer.classList.add('hidden');
        document.getElementById('write-box-title').innerText = "🔒 관리자 채널 (관리자만 작성 가능)";
      }
    } else {
      btnOpenWrite.classList.remove('hidden');
      writeFormContainer.classList.remove('hidden');
      document.getElementById('write-box-title').innerText = "✍️ 글쓰기";
    }
    document.getElementById('my-id-display').innerText = `ID:(${getMyUserId()})`;
    loadPosts();
  }
  function switchGallery(gal) {
    if (['g1', 'g2', 'g3'].includes(gal)) {
      const savedGrade = localStorage.getItem('saerom_verified_grade');
      const isAdminNow = localStorage.getItem('saerom_is_admin') === 'true';
      const requiredGrade = gal.replace('g', '');
      if (!isAdminNow && savedGrade !== requiredGrade) {
        alert(`접근 권한이 없습니다. (${requiredGrade}학년 이메일 인증 필요)`);
        return;
      }
    }
    currentGallery = gal;
    currentSearchKw = '';
    const searchInput = document.getElementById('search-kw');
    if (searchInput) searchInput.value = '';
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.getElementById('tab-' + gal);
    if (activeTab) activeTab.classList.add('active');
    document.getElementById('current-gallery-title').innerText = gal === 'home' ? '🏠 홈' : (galNames[gal] || '갤러리');
    closeView();
    // 갤러리를 바꿀 때마다 글쓰기 화면은 항상 닫힌 상태로 시작 (버튼을 눌러야 열림)
    document.getElementById('view-write').classList.add('hidden');
    if (gal === 'home') {
      document.getElementById('view-home').classList.remove('hidden');
      document.getElementById('view-list').classList.add('hidden');
      loadHomeSummary();
    } else {
      applyGalleryListUI(gal);
    }
  }
  // 📌 홈/인기글 카드에서 다른 갤러리의 글을 바로 열면, 화면 아래 목록이 그 글의
  //    갤러리로 맞춰지도록 동기화합니다 (이미 서버가 내려준 글을 보여주는 것뿐이라
  //    학년 갤러리 접근 권한 알림은 띄우지 않습니다 - 탭을 직접 눌러 들어갈 때만 검사합니다).
  function syncGalleryContextForPost(gal) {
    if (!gal || gal === currentGallery) return;
    currentGallery = gal;
    currentSearchKw = '';
    const searchInput = document.getElementById('search-kw');
    if (searchInput) searchInput.value = '';
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.getElementById('tab-' + gal);
    if (activeTab) activeTab.classList.add('active');
    document.getElementById('current-gallery-title').innerText = galNames[gal] || '갤러리';
    document.getElementById('view-write').classList.add('hidden');
    applyGalleryListUI(gal);
  }
  function changeSort(sortType) {
    currentSort = sortType;
    loadPosts();
  }
  function executeSearch() {
    currentSearchKw = document.getElementById('search-kw').value.trim();
    loadPosts();
  }
  function loadHomeSummary() {
    // 📌 서버 응답을 기다리는 동안 화면이 멈춘 것처럼 보이지 않도록 즉시 로딩 표시를 띄웁니다.
    ['home-concept-list', 'home-study-list', 'home-overseas-list', 'home-recent-list', 'home-dating-list'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<li style="color:#888;">불러오는 중...</li>';
    });
    google.colab.kernel.invokeFunction('notebook.get_home_summary', [], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        renderHomeList('home-concept-list', res.concepts);
        renderHomeList('home-study-list', res.studies);
        renderHomeList('home-overseas-list', res.overseas);
        renderHomeList('home-recent-list', res.recents);
      }
    });
    // 여소남소 갤러리 요약은 기존 get_posts API를 그대로 사용해 상위 4개만 표시
    google.colab.kernel.invokeFunction('notebook.get_posts', ['all', 'date', 'dating', ''], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        renderHomeList('home-dating-list', res.posts.slice(0, 4).map(p => ({...p, gallery: 'dating'})));
      }
    });
  }
  function renderHomeList(elemId, list) {
    const el = document.getElementById(elemId);
    el.innerHTML = list.length ? '' : '<li style="color:#888;">글이 없습니다.</li>';
    list.forEach(p => {
      let badge = p.is_concept ? '<span class="badge-concept">인기</span>' : '';
      if (p.gallery === 'admin_notice') badge = '<span class="badge-admin">공지</span>';
      el.innerHTML += `<li onclick="viewPost(${p.id})">
        <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:80%;">
          ${badge} ${escapeHtml(p.title)} <span class="comment-count">[${p.comment_count}]</span>
        </span>
        <span style="color:#888;">${p.date}</span>
      </li>`;
    });
  }
  function renderPostTable(posts) {
    const tbody = document.getElementById('post-list');
    tbody.innerHTML = '';
    if (posts.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:#888; padding:15px;">게시글이 없습니다.</td></tr>';
      return;
    }
    posts.forEach(post => {
      const tr = document.createElement('tr');
      const imgBadge = post.has_image ? ' 📷' : '';
      let gBadge = `<span class="badge-gal">${galNames[post.gallery] || '기타'}</span>`;
      if (post.gallery === 'admin_notice') gBadge = `<span class="badge-admin">공지</span>`;
      tr.innerHTML = `
        <td>${post.local_id}</td>
        <td class="title-td" onclick="viewPost(${post.id})">
          ${gBadge} ${post.is_concept ? '<span class="badge-concept">인기</span>' : ''}
          ${escapeHtml(post.title)}${imgBadge} <span class="comment-count">[${post.comment_count}]</span>
        </td>
        <td>${escapeHtml(post.author)}<span class="user-id">(${displayAuthorId(post.author_id)})</span></td>
        <td>${post.upvotes}</td>
      `;
      tbody.appendChild(tr);
    });
  }
  function loadPosts() {
    const tabType = (currentGallery === 'concept') ? 'concept' : 'all';
    const galType = (currentGallery === 'concept' || currentGallery === 'all') ? 'all_global' : currentGallery;
    const tbody = document.getElementById('post-list');
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" style="color:#888; padding:15px;">불러오는 중...</td></tr>';
    google.colab.kernel.invokeFunction('notebook.get_posts', [tabType, currentSort, galType, currentSearchKw], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) renderPostTable(res.posts);
      else if (tbody) tbody.innerHTML = '<tr><td colspan="4" style="color:#e74c3c; padding:15px;">불러오지 못했습니다. 새로고침 해주세요.</td></tr>';
    });
  }
  // 📌 저장(즐겨찾기)한 글 목록 - localStorage에 저장된 글 id들만 서버에서 가져와 보여줌
  function getSavedIds() {
    try { return JSON.parse(localStorage.getItem('saerom_saved_posts') || '[]'); } catch (e) { return []; }
  }
  function setSavedIds(ids) {
    localStorage.setItem('saerom_saved_posts', JSON.stringify(ids));
  }
  function isSaved(postId) {
    return getSavedIds().includes(postId);
  }
  function toggleSavePost() {
    if (!currentPostId) return;
    if (!isLoggedIn()) { alert("게시글 저장은 로그인 후 이용 가능합니다."); return; }
    let ids = getSavedIds();
    if (ids.includes(currentPostId)) {
      ids = ids.filter(i => i !== currentPostId);
    } else {
      ids.push(currentPostId);
    }
    setSavedIds(ids);
    refreshSaveButton();
  }
  function refreshSaveButton() {
    const btn = document.getElementById('btn-save');
    if (!btn || !currentPostId) return;
    if (isSaved(currentPostId)) {
      btn.innerText = '★ 저장됨';
      btn.style.background = '#f39c12';
    } else {
      btn.innerText = '☆ 저장';
      btn.style.background = '#95a5a6';
    }
  }
  function loadSavedPosts() {
    const ids = getSavedIds();
    const tbody = document.getElementById('post-list');
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" style="color:#888; padding:15px;">불러오는 중...</td></tr>';
    google.colab.kernel.invokeFunction('notebook.get_posts_by_ids', [ids], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) renderPostTable(res.posts);
      else if (tbody) tbody.innerHTML = '<tr><td colspan="4" style="color:#e74c3c; padding:15px;">불러오지 못했습니다. 새로고침 해주세요.</td></tr>';
    });
  }
  // 📌 광고판 이미지
  function setAdAdminUploadVisible(isAdmin) {
    [1, 2].forEach(slot => {
      const box = document.getElementById('ad-admin-upload-' + slot);
      if (box) box.classList.toggle('hidden', !isAdmin);
    });
  }
  function loadAdBanner(slot) {
    google.colab.kernel.invokeFunction('notebook.get_ad_banner', [slot], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success && res.image_url) {
        document.getElementById('ad-banner-placeholder-' + slot).classList.add('hidden');
        const img = document.getElementById('ad-banner-img-' + slot);
        img.src = res.image_url;
        img.classList.remove('hidden');
      }
    });
  }
  function uploadAdBanner(slot) {
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    if (!isAdmin) { alert("관리자만 이용할 수 있습니다."); return; }
    const fileInput = document.getElementById('ad-file-' + slot);
    if (!fileInput.files || !fileInput.files[0]) { alert("이미지 파일을 선택해주세요."); return; }
    const reader = new FileReader();
    reader.onload = function(e) {
      google.colab.kernel.invokeFunction('notebook.set_ad_banner', [slot, e.target.result, getMyUserId(), isAdmin], {}).then(obj => {
        const res = parseRes(obj);
        alert(res.msg);
        if (res.success) {
          fileInput.value = '';
          loadAdBanner(slot);
        }
      });
    };
    reader.readAsDataURL(fileInput.files[0]);
  }
  function submitPost() {
    if (!isLoggedIn()) { alert("글쓰기는 로그인 후 이용 가능합니다."); return; }
    const btn = document.getElementById('btn-submit-post');
    if (btn.disabled) return;
    const a = document.getElementById('post-author').value;
    const t = document.getElementById('post-title').value, c = document.getElementById('post-content').value;
    const fileInput = document.getElementById('post-file');
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    if (!t.trim() || !c.trim()) { alert("내용을 입력하세요."); return; }
    btn.disabled = true;
    btn.innerText = editingPostId ? "수정 중..." : "등록 중...";
    const finish = () => {
      btn.disabled = false;
      btn.innerText = editingPostId ? "수정 완료" : "등록";
    };
    if (fileInput.files && fileInput.files[0]) {
      const reader = new FileReader();
      reader.onload = function(e) {
        if (editingPostId) sendEditApi(t, c, e.target.result, finish);
        else sendPostApi(t, c, a, e.target.result, isAdmin, finish);
      };
      reader.readAsDataURL(fileInput.files[0]);
    } else {
      if (editingPostId) sendEditApi(t, c, '', finish);
      else sendPostApi(t, c, a, '', isAdmin, finish);
    }
  }
  function sendPostApi(title, content, author, imageUrl, isAdmin, callback) {
    google.colab.kernel.invokeFunction('notebook.add_post', [title, content, author, getMyUserId(), '', currentGallery, imageUrl, isAdmin], {}).then(obj => {
      if (callback) callback();
      const res = parseRes(obj);
      if (res.success) {
        document.getElementById('post-title').value = '';
        document.getElementById('post-content').value = '';
        document.getElementById('post-file').value = '';
        closeWriteBox();
        loadPosts();
      } else {
        alert(res.msg);
      }
    }).catch(() => { if (callback) callback(); });
  }
  function sendEditApi(title, content, imageUrl, callback) {
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    const postId = editingPostId;
    google.colab.kernel.invokeFunction('notebook.edit_post', [postId, title, content, getMyUserId(), imageUrl, isAdmin], {}).then(obj => {
      if (callback) callback();
      const res = parseRes(obj);
      if (res.success) {
        document.getElementById('post-title').value = '';
        document.getElementById('post-content').value = '';
        document.getElementById('post-file').value = '';
        closeWriteBox();
        viewPost(postId);
        loadPosts();
      } else {
        alert(res.msg);
      }
    }).catch(() => { if (callback) callback(); });
  }
  function deleteCurrentPost() {
    if (!currentPostId) return;
    if (!isLoggedIn()) { alert("로그인 후 이용 가능합니다."); return; }
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    // 📌 글쓰기 자체가 로그인 필수라 작성자가 누구인지 이미 알고 있으므로,
    //    삭제도 비밀번호 대신 "본인 글인지"로 서버가 판단합니다.
    const confirmMsg = isAdmin ? "👑 [관리자 권한] 이 게시글을 즉시 삭제하시겠습니까?" : "이 게시글을 삭제하시겠습니까?";
    if (!confirm(confirmMsg)) return;
    google.colab.kernel.invokeFunction('notebook.delete_post', [currentPostId, getMyUserId(), isAdmin], {}).then(obj => {
      const res = parseRes(obj);
      alert(res.msg);
      if (res.success) {
        closeView();
        if (currentGallery === 'home') loadHomeSummary(); else loadPosts();
      }
    });
  }
  // 📌 댓글 목록 렌더링 (최초 진입 시 + 아래 실시간 자동 새로고침에서 공용으로 사용)
  function renderComments(comments) {
    const cmtList = document.getElementById('comment-list');
    if (!cmtList) return;
    cmtList.innerHTML = comments.length ? '' : '<div style="color:#888; padding:4px 0;">첫 댓글을 남겨보세요!</div>';
    comments.forEach(cmt => {
      let imgHtml = cmt.image_url ? `<img src="${cmt.image_url}" class="comment-img">` : '';
      cmtList.innerHTML += `<div class="comment-item">
        <div style="flex:1; padding-right:6px;">
          <div><b>${escapeHtml(cmt.author)}</b><span class="user-id">(${displayAuthorId(cmt.author_id)})</span> <span style="font-size:9px; color:#999;">${cmt.date}</span></div>
          <div class="comment-body">${escapeHtml(cmt.content)}</div>
          ${imgHtml}
        </div>
        <div style="flex-shrink:0;">
          <button class="dc-btn dc-btn-danger btn-compact" onclick="reportComment(${cmt.id})">신고</button>
        </div>
      </div>`;
    });
  }
  // 📌 실시간 소통을 위한 자동 새로고침(폴링): 완전한 실시간 웹소켓 대신,
  //    게시글을 보는 동안엔 댓글/추천수를, 목록을 보는 동안엔 새 글 목록을
  //    주기적으로 다시 불러와 "다른 사람이 쓴 글/댓글"이 곧바로 반영되게 합니다.
  let detailPollTimer = null;
  function startDetailPolling() {
    stopDetailPolling();
    detailPollTimer = setInterval(() => {
      if (!currentPostId) return;
      google.colab.kernel.invokeFunction('notebook.get_post_detail', [currentPostId, false], {}).then(obj => {
        const res = parseRes(obj);
        if (!res.success || !currentPostId) return;
        const p = res.post;
        currentPostData = p;
        document.getElementById('up-count').innerText = p.upvotes;
        document.getElementById('down-count').innerText = p.downvotes;
        renderComments(res.comments);
      });
    }, 5000);
  }
  function stopDetailPolling() {
    if (detailPollTimer) { clearInterval(detailPollTimer); detailPollTimer = null; }
  }
  let listPollTimer = null;
  function startListPolling() {
    stopListPolling();
    listPollTimer = setInterval(() => {
      const vList = document.getElementById('view-list');
      const vWrite = document.getElementById('view-write');
      const vHome = document.getElementById('view-home');
      if (currentPostId) return; // 상세글을 보고 있을 땐 목록 폴링 대신 댓글 폴링만
      if (currentGallery === 'home' && vHome && !vHome.classList.contains('hidden')) {
        loadHomeSummary();
      } else if (currentGallery !== 'saved' && vList && !vList.classList.contains('hidden') && vWrite && vWrite.classList.contains('hidden')) {
        loadPosts();
      }
    }, 8000);
  }
  function stopListPolling() {
    if (listPollTimer) { clearInterval(listPollTimer); listPollTimer = null; }
  }
  function viewPost(postId) {
    currentPostId = postId;
    // 📌 서버 응답을 기다리는 동안 클릭이 씹힌 것처럼 보이지 않도록, 요청 즉시
    //    로딩 상태를 먼저 보여줍니다 (특히 Render 무료 플랜은 첫 응답이 몇십 초 걸릴 수 있음).
    document.getElementById('view-title').innerText = '불러오는 중...';
    document.getElementById('view-meta').innerText = '';
    document.getElementById('view-content').innerText = '';
    document.getElementById('view-image-container').innerHTML = '';
    document.getElementById('comment-list').innerHTML = '';
    document.getElementById('post-view-box').classList.remove('hidden');
    const contentAreaEarly = document.querySelector('.dc-content');
    if (contentAreaEarly) contentAreaEarly.scrollTop = 0;
    document.getElementById('post-view-box').scrollIntoView({ block: 'start' });
    google.colab.kernel.invokeFunction('notebook.get_post_detail', [postId, true], {}).then(obj => {
      const res = parseRes(obj);
      if (res.success) {
        const p = res.post;
        currentPostData = p;
        // 📌 홈/인기글 카드에서 다른 갤러리의 글을 바로 열었을 때, 화면 아래 목록이
        //    엉뚱한 갤러리 내용으로 남아있지 않도록 이 글의 갤러리로 맞춰줍니다.
        syncGalleryContextForPost(p.gallery);
        let tagHtml = `<span class="badge-gal">${galNames[p.gallery] || '기타'}</span> `;
        if (p.gallery === 'admin_notice') tagHtml = `<span class="badge-admin">공지</span> `;
        if (p.is_concept) tagHtml += `<span class="badge-concept">인기</span> `;
        document.getElementById('view-title').innerHTML = tagHtml + escapeHtml(p.title);
        document.getElementById('view-meta').innerText = `${p.author}(${displayAuthorId(p.author_id)}) | ${p.date} | 조회 ${p.views}`;
        document.getElementById('view-content').innerText = p.content;
        const imgBox = document.getElementById('view-image-container');
        imgBox.innerHTML = p.image_url ? `<img src="${p.image_url}" class="post-img">` : '';
        document.getElementById('up-count').innerText = p.upvotes;
        document.getElementById('down-count').innerText = p.downvotes;
        renderComments(res.comments);
        const loggedIn = isLoggedIn();
        document.getElementById('comment-write-area').classList.toggle('hidden', !loggedIn);
        document.getElementById('comment-login-notice').classList.toggle('hidden', loggedIn);
        const isAdminNow = localStorage.getItem('saerom_is_admin') === 'true';
        const canEdit = loggedIn && (getMyUserId() === p.author_id || isAdminNow);
        const editBtn = document.getElementById('btn-edit');
        if (editBtn) editBtn.style.display = canEdit ? 'inline-block' : 'none';
        // 📌 삭제도 수정과 동일하게 "본인 글 또는 관리자"만 버튼이 보이도록 합니다.
        const deleteBtn = document.getElementById('btn-delete');
        if (deleteBtn) deleteBtn.style.display = canEdit ? 'inline-block' : 'none';
        refreshSaveButton();
        document.getElementById('post-view-box').classList.remove('hidden');
        // 📌 detail 영역이 항상 목록 위쪽에 있으므로, 목록에서 다른 글을 눌렀을 때도
        //    실제로 화면이 이동한 것처럼 보이도록 콘텐츠 영역을 맨 위로 스크롤합니다.
        const contentArea = document.querySelector('.dc-content');
        if (contentArea) contentArea.scrollTop = 0;
        document.getElementById('post-view-box').scrollIntoView({ block: 'start' });
        startDetailPolling();
      } else {
        document.getElementById('view-title').innerText = '글을 불러오지 못했습니다.';
        document.getElementById('view-content').innerText = res.msg || '잠시 후 다시 시도해주세요.';
      }
    });
  }
  function vote(type) {
    if (!currentPostId) return;
    const key = 'saerom_voted_' + currentPostId;
    if (localStorage.getItem(key)) { alert("이미 참여하셨습니다!"); return; }
    // 📌 연타 방지: 서버 응답을 기다리지 않고 요청 즉시 잠그고 버튼을 비활성화합니다.
    localStorage.setItem(key, type);
    const voteButtons = document.querySelectorAll('.btn-vote');
    voteButtons.forEach(b => b.disabled = true);
    google.colab.kernel.invokeFunction('notebook.vote_post', [currentPostId, type], {}).then(obj => {
      voteButtons.forEach(b => b.disabled = false);
      if (parseRes(obj).success) {
        viewPost(currentPostId);
        if (currentGallery === 'home') loadHomeSummary(); else loadPosts();
      } else {
        localStorage.removeItem(key);
      }
    }).catch(() => {
      voteButtons.forEach(b => b.disabled = false);
      localStorage.removeItem(key);
    });
  }
  function submitComment() {
    if (!currentPostId) return;
    if (!isLoggedIn()) { alert("댓글 작성은 로그인 후 이용 가능합니다."); return; }
    const btn = document.getElementById('btn-submit-cmt');
    if (btn.disabled) return;
    const a = document.getElementById('cmt-author').value, c = document.getElementById('cmt-content').value;
    const fileInput = document.getElementById('cmt-file');
    btn.disabled = true;
    btn.innerText = "등록 중...";
    const finish = () => {
      btn.disabled = false;
      btn.innerText = "댓글 등록";
    };
    if (fileInput.files && fileInput.files[0]) {
      const reader = new FileReader();
      reader.onload = function(e) { sendCommentApi(c, a, e.target.result, finish); };
      reader.readAsDataURL(fileInput.files[0]);
    } else {
      sendCommentApi(c, a, '', finish);
    }
  }
  function sendCommentApi(content, author, imageUrl, callback) {
    google.colab.kernel.invokeFunction('notebook.add_comment', [currentPostId, content, author, getMyUserId(), '', imageUrl], {}).then(obj => {
      if (callback) callback();
      if (parseRes(obj).success) {
        document.getElementById('cmt-content').value = '';
        document.getElementById('cmt-file').value = '';
        viewPost(currentPostId);
      }
    }).catch(() => { if (callback) callback(); });
  }
  function reportCurrentPost() {
    if (!currentPostId) return;
    const reason = prompt("게시글 신고 사유를 입력하세요:");
    if (!reason || !reason.trim()) return;
    google.colab.kernel.invokeFunction('notebook.report_item', ['게시글', currentPostId, reason.trim()], {}).then(obj => {
      const res = parseRes(obj);
      alert(res.msg || "신고가 접수되었습니다.");
    });
  }
  function reportComment(commentId) {
    const reason = prompt("댓글 신고 사유를 입력하세요:");
    if (!reason || !reason.trim()) return;
    google.colab.kernel.invokeFunction('notebook.report_item', ['댓글', commentId, reason.trim()], {}).then(obj => {
      const res = parseRes(obj);
      alert(res.msg || "신고가 접수되었습니다.");
    });
  }
  function closeView() {
    document.getElementById('post-view-box').classList.add('hidden');
    currentPostId = null;
    stopDetailPolling();
  }
  function escapeHtml(str) { return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  // 📌 일반 사용자에게는 실제 학번 대신 익명화된 번호만 보여주고, 관리자에게만 실제 학번을 보여줍니다.
  function displayAuthorId(id) {
    const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
    if (isAdmin) return id;
    const str = String(id || '');
    let hash = 0;
    for (let i = 0; i < str.length; i++) { hash = (hash * 31 + str.charCodeAt(i)) % 900; }
    return String(hash + 100);
  }
  // 📌 window.onload("load" 이벤트)는 이미지 등 모든 리소스가 완전히 로드된 뒤에야
  //    발생해서, 경우에 따라 시계가 한참 동안 "로딩 중..."에 멈춰있는 것처럼 보일 수
  //    있습니다. 이 <script>는 body 맨 끝에 있어 DOM은 이미 다 준비된 상태이므로,
  //    load 이벤트를 기다리지 말고 바로 초기화합니다.
  function initApp() {
    startClock();
    try {
      loadAdBanner(1);
      loadAdBanner(2);
      const savedGrade = localStorage.getItem('saerom_verified_grade');
      const isAdmin = localStorage.getItem('saerom_is_admin') === 'true';
      if (savedGrade) applyGradeUI(savedGrade, isAdmin);
      setAdAdminUploadVisible(isAdmin);
      switchGallery('home');
      startListPolling();
    } catch (e) {
      console.error('온리새롬 갤러리 초기화 중 오류:', e);
    }
  }
  // 📌 사이트 첫 진입 시 로고 인트로 영상을 보여주고, 영상이 끝나거나(또는 재생이
  //    막히거나) 사용자가 건너뛰기를 누르면 오버레이를 서서히 사라지게 합니다.
  function hideIntro() {
    const overlay = document.getElementById('intro-overlay');
    if (!overlay || overlay.classList.contains('hide')) return;
    overlay.classList.add('hide');
    setTimeout(() => { overlay.style.display = 'none'; }, 650);
  }
  function skipIntro() {
    const video = document.getElementById('intro-video');
    if (video) { try { video.pause(); } catch (e) {} }
    hideIntro();
  }
  (function initIntro() {
    const video = document.getElementById('intro-video');
    if (!video) return;
    video.addEventListener('ended', hideIntro);
    video.addEventListener('error', hideIntro);
    // 일부 브라우저는 자동재생을 막을 수 있으므로, 그런 경우 곧바로 오버레이를 닫습니다.
    const playPromise = video.play();
    if (playPromise && playPromise.catch) playPromise.catch(hideIntro);
    // 안전장치: 영상 길이(4초) 기준, 혹시 ended 이벤트를 못 받아도 6초 뒤엔 무조건 닫습니다.
    setTimeout(hideIntro, 6000);
  })();
  initApp();
</script>
</body>
</html>
"""
HTML_PAGE = HTML_PAGE.replace('{{INTRO_VIDEO_B64}}', INTRO_VIDEO_B64)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
