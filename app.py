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
    .dc-header { background: #454D80; color: white; padding: 8px 12px; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
    .dc-header h1 { margin: 0; font-size: 15px; cursor: pointer; color: #ffffff; }
    .dc-header-right { display: flex; align-items: center; gap: 8px; }
    .dc-clock { font-size: 12px; background: #32385C; color: #2ee6b5; padding: 3px 9px; border-radius: 3px; font-weight: bold; }
    .dc-header span.gal-tag { font-size: 10px; background: #32385C; padding: 2px 6px; border-radius: 3px; }
    .nav-tabs { display: flex; background: #32385C; flex-shrink: 0; overflow-x: auto; }
    .nav-tab { flex: 1; text-align: center; padding: 8px 4px; color: #ccc; font-size: 11px; font-weight: bold; cursor: pointer; white-space: nowrap; min-width: 55px; }
    .nav-tab.active { background: #fff; color: #454D80; }
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
    .dc-title { font-size: 11px; font-weight: bold; color: #454D80; margin-bottom: 6px; border-bottom: 1px solid #454D80; padding-bottom: 3px; display: flex; justify-content: space-between; align-items: center; }
    .input-row { display: flex; gap: 4px; margin-bottom: 4px; }
    input, textarea, select { padding: 4px 6px; border: 1px solid #ccc; font-size: 11px; font-family: inherit; }
    input.auth-input { width: 50%; }
    input.full-input { width: 100%; margin-bottom: 4px; }
    textarea { width: 100%; height: 48px; resize: none; margin-bottom: 4px; line-height: 1.4; }
    .dc-btn { padding: 4px 8px; background: #454D80; color: white; border: none; font-size: 11px; font-weight: bold; cursor: pointer; border-radius: 2px; }
    .dc-btn:disabled { background: #888 !important; cursor: not-allowed; }
    .dc-btn-write { background: #454D80; width: 100%; padding: 6px; }
    .dc-btn-open-write { background: #454D80; font-size: 11px; padding: 5px 12px; white-space: nowrap; }
    .dc-btn-danger { background: #e74c3c; }
    .dc-btn-delete { background: #555; }
    .btn-compact { font-size: 10px; padding: 2px 6px; }
    .post-action-btn { font-size: 11px; padding: 4px 10px; margin-left: 4px; }
    .dc-btn-admin-req { background: #16a085; width: 100%; font-size: 11px; padding: 6px; font-weight: bold; }
    .hidden { display: none !important; }
    /* 📌 텍스트 규격 및 테이블 최적화 */
    .dc-table { width: 100%; border-collapse: collapse; font-size: 11px; text-align: center; table-layout: fixed; }
    .dc-table th { background: #f2f2f2; border-top: 1px solid #454D80; border-bottom: 1px solid #ddd; padding: 6px 2px; }
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
    .home-card-header { font-size: 11px; font-weight: bold; color: #454D80; border-bottom: 1.5px solid #454D80; padding-bottom: 3px; margin-bottom: 4px; display: flex; justify-content: space-between; }
    .home-list { list-style: none; padding: 0; margin: 0; font-size: 11px; }
    .home-list li { padding: 4px 0; border-bottom: 1px solid #f1f5f9; display: flex; justify-content: space-between; cursor: pointer; word-break: break-all; }
    /* 📌 게시글 및 댓글 텍스트 줄바꿈 / 자간 / 정렬 규격 개선 */
    .post-view { border: 1px solid #454D80; background: #fff; padding: 10px; margin-bottom: 8px; }
    .post-view-header { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 1px dashed #ccc; padding-bottom: 6px; }
    .post-view-title { font-size: 13px; font-weight: bold; color: #454D80; word-break: keep-all; word-wrap: break-word; line-height: 1.4; }
    .post-view-meta { font-size: 10px; color: #888; margin: 4px 0 8px 0; }
    .post-view-content { font-size: 12px; line-height: 1.6; color: #222; min-height: 50px; white-space: pre-wrap; word-break: keep-all; word-wrap: break-word; overflow-wrap: break-word; margin-bottom: 10px; }
    .post-img { max-width: 100%; max-height: 260px; display: block; margin: 8px 0; border: 1px solid #ddd; }
    .vote-box { display: flex; justify-content: center; gap: 8px; margin: 10px 0; }
    .btn-vote { display: flex; flex-direction: column; align-items: center; justify-content: center; width: 52px; height: 38px; border: 1px solid #ccc; background: #fdfdfd; cursor: pointer; border-radius: 4px; font-size: 10px; }
    .btn-vote.up { border-color: #454D80; color: #454D80; }
    .btn-vote:disabled { opacity: 0.5; cursor: not-allowed; }
    /* 📌 댓글 영역 텍스트 및 간격 핏 조율 */
    .comment-section { border-top: 1px solid #454D80; padding-top: 6px; background: #fafafa; padding: 8px; }
    .comment-list { list-style: none; padding: 0; margin: 0 0 8px 0; }
    .comment-item { border-bottom: 1px solid #e5e7eb; padding: 5px 0; font-size: 11px; display: flex; justify-content: space-between; line-height: 1.4; }
    .comment-body { word-break: keep-all; word-wrap: break-word; overflow-wrap: break-word; white-space: pre-wrap; margin-top: 2px; color: #333; }
    .comment-img { max-width: 100%; max-height: 120px; display: block; margin-top: 4px; border: 1px solid #ddd; }
    .toolbar-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; gap: 6px; }
    .search-box { display: flex; gap: 2px; flex: 1; }
    .search-input { width: 100%; padding: 3px 6px; font-size: 10px; }
    .sort-select { padding: 2px 4px; font-size: 10px; border: 1px solid #454D80; background: #fff; color: #454D80; font-weight: bold; }
    .status-badge { font-size: 10px; padding: 3px 6px; background: #eef2ff; color: #454D80; border: 1px solid #454D80; border-radius: 3px; margin-bottom: 6px; text-align: center; }
    .notice-box-text { font-size: 11px; color: #555; line-height: 1.5; }
    /* 모달 모듈 (관리자 요청) */
    .modal-backdrop { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .modal-box { background: #fff; border: 2px solid #454D80; width: 320px; padding: 12px; border-radius: 4px; box-shadow: 0 4px 10px rgba(0,0,0,0.15); }
  </style>
</head>
<body>
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
        <button class="dc-btn" style="width:100%; background:#454D80;" onclick="uploadAdBanner(1)">광고란 1에 등록</button>
      </div>
      <div class="dc-title" style="margin-bottom:2px; margin-top:6px;">📣 광고란 2</div>
      <div id="ad-banner-box-2" class="ad-banner-box ad-banner-box-2">
        <div id="ad-banner-placeholder-2">광고 이미지 없음<br>광고 관련 문의는 관리자에게</div>
        <img id="ad-banner-img-2" class="hidden">
      </div>
      <!-- 📌 관리자 전용 광고란 2 이미지 등록 -->
      <div id="ad-admin-upload-2" class="hidden">
        <input type="file" id="ad-file-2" accept="image/*" style="font-size:9px; width:100%; margin-bottom:4px;">
        <button class="dc-btn" style="width:100%; background:#454D80;" onclick="uploadAdBanner(2)">광고란 2에 등록</button>
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
          <div style="font-size:11px; font-weight:bold; color:#454D80; margin-bottom:4px;">💬 댓글</div>
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
  initApp();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
