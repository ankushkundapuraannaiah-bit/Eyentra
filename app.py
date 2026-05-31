"""
Eyentra - Secure Photo Sharing with QR Codes
================================================
A Flask web application that lets users register with their mobile number,
upload photos, share them via QR codes, and track who viewed them.
"""

import os
import io
import uuid
import random
import string
import base64
from datetime import datetime, timedelta

import qrcode
from PIL import Image

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


# ─────────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-to-something-very-secret-in-production")

# Directories (only used in local dev; Vercel filesystem is read-only)
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
QR_DIR     = os.path.join(BASE_DIR, "static", "qrcodes")

# Create local dirs only when NOT on Vercel
if not os.environ.get("VERCEL"):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(QR_DIR,     exist_ok=True)

# ── Database configuration ──────────────────────────────────────────────────
# Prefer PostgreSQL env vars (Vercel / Neon inject one of these).
# Fall back to SQLite for local development only.
database_url = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_PRISMA_URL")
    or os.environ.get("STORAGE_URL")
)

if database_url:
    # Normalise legacy "postgres://" scheme
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    # Strip query params (e.g. ?sslmode=require&channel_binding=require) —
    # pg8000 doesn't accept them in the URL; we pass ssl separately via connect_args
    base_url = database_url.split("?")[0]
    # Use pg8000 driver (pure Python, no C extensions needed on Vercel)
    if "postgresql+pg8000" not in base_url:
        base_url = base_url.replace("postgresql://", "postgresql+pg8000://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = base_url
    # Neon requires SSL — pass it via connect_args instead of the URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"ssl_context": True}
    }
    print(f"[DB] Using PostgreSQL (pg8000): {base_url.split('@')[-1]}")  # log host only
else:
    if os.environ.get("VERCEL"):
        print("WARNING: No DATABASE_URL found. Vercel deployment will fail without PostgreSQL.")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'eyentra.db')}"
    print("[DB] Using local SQLite")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"]             = 10 * 1024 * 1024   # 10 MB limit

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

db = SQLAlchemy(app)


# ─────────────────────────────────────────────
#  Database models
# ─────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"

    id          = db.Column(db.Integer,     primary_key=True)
    mobile      = db.Column(db.String(15),  unique=True, nullable=False)
    password    = db.Column(db.String(256), nullable=False)
    is_verified = db.Column(db.Boolean,     default=False)
    created_at  = db.Column(db.DateTime,    default=datetime.utcnow)

    photos    = db.relationship("Photo",   backref="owner",  lazy=True)
    view_logs = db.relationship("ViewLog", backref="viewer", lazy=True)


class OTPRecord(db.Model):
    __tablename__ = "otp_records"

    id         = db.Column(db.Integer,    primary_key=True)
    mobile     = db.Column(db.String(15), nullable=False)
    otp        = db.Column(db.String(6),  nullable=False)
    expires_at = db.Column(db.DateTime,   nullable=False)
    used       = db.Column(db.Boolean,    default=False)


class Photo(db.Model):
    __tablename__ = "photos"

    id             = db.Column(db.Integer,     primary_key=True)
    user_id        = db.Column(db.Integer,     db.ForeignKey("users.id"), nullable=False)
    filename       = db.Column(db.String(256), nullable=False)
    original_name  = db.Column(db.String(256), nullable=False)
    share_password = db.Column(db.String(20),  nullable=False)
    share_token    = db.Column(db.String(64),  unique=True, nullable=False)
    qr_filename    = db.Column(db.String(256), nullable=True)
    file_blob      = db.Column(db.LargeBinary, nullable=True)   # stores image bytes in production
    qr_blob        = db.Column(db.LargeBinary, nullable=True)   # stores QR bytes in production
    uploaded_at    = db.Column(db.DateTime,    default=datetime.utcnow)

    view_logs = db.relationship("ViewLog", backref="photo", lazy=True)


class ViewLog(db.Model):
    __tablename__ = "view_logs"

    id            = db.Column(db.Integer,    primary_key=True)
    photo_id      = db.Column(db.Integer,    db.ForeignKey("photos.id"), nullable=False)
    viewer_id     = db.Column(db.Integer,    db.ForeignKey("users.id"),  nullable=True)
    view_id       = db.Column(db.String(64), unique=True, nullable=False)
    viewer_mobile = db.Column(db.String(15), nullable=True)
    viewed_at     = db.Column(db.DateTime,   default=datetime.utcnow)
    notified      = db.Column(db.Boolean,    default=False)


# ─────────────────────────────────────────────
#  Helper functions
# ─────────────────────────────────────────────

def generate_otp():
    return str(random.randint(100000, 999999))


def generate_share_password(length=10):
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def generate_share_token():
    return str(uuid.uuid4()).replace("-", "")


def generate_view_id():
    return "VW-" + str(uuid.uuid4())[:8].upper()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def send_otp(mobile, otp):
    """
    Send OTP via Fast2SMS (free, India) if FAST2SMS_API_KEY is set.
    Falls back to Twilio if TWILIO_* vars are set.
    Otherwise shows OTP in the UI (dev mode).
    Returns the OTP string if simulating, None if sent via gateway.
    """
    import urllib.request, json as _json

    # ── Fast2SMS (free Indian SMS gateway) ──────────────────────────
    fast2sms_key = os.environ.get("FAST2SMS_API_KEY")
    if fast2sms_key:
        try:
            payload = _json.dumps({
                "route":    "otp",
                "variables_values": otp,
                "numbers":  mobile,
            }).encode()
            req = urllib.request.Request(
                "https://www.fast2sms.com/dev/bulkV2",
                data=payload,
                headers={
                    "authorization": fast2sms_key,
                    "Content-Type":  "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
            if result.get("return"):
                print(f"[SMS] OTP sent to {mobile} via Fast2SMS")
                return None
            else:
                print(f"[SMS] Fast2SMS error: {result} — falling back")
        except Exception as e:
            print(f"[SMS] Fast2SMS exception: {e} — falling back")

    # ── Twilio ───────────────────────────────────────────────────────
    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from  = os.environ.get("TWILIO_FROM_NUMBER")
    if twilio_sid and twilio_token and twilio_from:
        try:
            from twilio.rest import Client
            client = Client(twilio_sid, twilio_token)
            client.messages.create(
                body=f"Your Eyentra OTP is: {otp}. Valid for 10 minutes.",
                from_=twilio_from,
                to=f"+91{mobile}" if not mobile.startswith("+") else mobile,
            )
            print(f"[SMS] OTP sent to {mobile} via Twilio")
            return None
        except Exception as e:
            print(f"[SMS] Twilio error: {e} — falling back to simulation")

    # ── Dev / simulation mode ────────────────────────────────────────
    print(f"\n{'='*50}")
    masked = f"******{mobile[-4:]}" if len(mobile) > 4 else mobile
    print(f"  [SMS SIMULATION]  To: {masked}   OTP: {otp}")
    print(f"{'='*50}\n")
    return otp   # caller flashes this to the user


def make_qr_code(share_url, photo_id):
    """Generate a QR code. Returns (filename, blob_bytes)."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(share_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Always capture bytes (needed for DB storage on Vercel)
    img_io = io.BytesIO()
    img.save(img_io, "PNG")
    qr_blob = img_io.getvalue()

    qr_filename = f"qr_{photo_id}_{uuid.uuid4().hex[:8]}.png"

    # Also save to disk in local dev
    if not os.environ.get("VERCEL"):
        img.save(os.path.join(QR_DIR, qr_filename))

    return qr_filename, qr_blob


def current_user():
    user_id = session.get("user_id")
    if user_id:
        return User.query.get(user_id)
    return None


def login_required_redirect(endpoint="login"):
    if not session.get("user_id"):
        flash("Please log in first.", "warning")
        return redirect(url_for(endpoint))
    return None


# ─────────────────────────────────────────────
#  Routes — Health check
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        User.query.limit(1).all()
        return jsonify({"status": "healthy", "database": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


# ─────────────────────────────────────────────
#  Routes — Public pages
# ─────────────────────────────────────────────

@app.route("/")
def index():
    user = current_user()
    return render_template("index.html", user=user)


# ─────────────────────────────────────────────
#  Routes — Authentication
# ─────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        mobile   = request.form.get("mobile",   "").strip()
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm",  "").strip()

        if not mobile or not mobile.isdigit() or len(mobile) < 10:
            flash("Please enter a valid mobile number (at least 10 digits).", "danger")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        existing = User.query.filter_by(mobile=mobile).first()
        if existing and existing.is_verified:
            flash("This mobile number is already registered. Please log in.", "warning")
            return redirect(url_for("login"))

        if not existing:
            new_user = User(mobile=mobile, password=generate_password_hash(password), is_verified=False)
            db.session.add(new_user)
            db.session.commit()
        else:
            existing.password    = generate_password_hash(password)
            existing.is_verified = False
            db.session.commit()

        otp = generate_otp()
        db.session.add(OTPRecord(
            mobile=mobile,
            otp=otp,
            expires_at=datetime.utcnow() + timedelta(minutes=10)
        ))
        db.session.commit()

        sim_otp = send_otp(mobile, otp)
        session["pending_mobile"] = mobile
        if sim_otp:
            # No SMS gateway configured — show OTP directly in the UI
            flash(f"[DEV MODE] OTP for {mobile}: {sim_otp}  — Enter this below to verify.", "warning")
        else:
            flash(f"OTP sent to your mobile number. Enter it below to verify.", "info")
        return redirect(url_for("verify_otp"))

    return render_template("register.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    mobile = session.get("pending_mobile")
    if not mobile:
        return redirect(url_for("register"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()

        record = (
            OTPRecord.query
            .filter_by(mobile=mobile, used=False)
            .filter(OTPRecord.expires_at > datetime.utcnow())
            .order_by(OTPRecord.id.desc())
            .first()
        )

        if not record:
            flash("OTP has expired. Please register again.", "danger")
            return redirect(url_for("register"))

        if record.otp != entered_otp:
            flash("Incorrect OTP. Please try again.", "danger")
            return render_template("verify_otp.html", mobile=mobile)

        record.used = True
        user = User.query.filter_by(mobile=mobile).first()
        user.is_verified = True
        db.session.commit()

        session.pop("pending_mobile", None)
        session["user_id"] = user.id
        flash("Mobile verified! Welcome to Eyentra.", "success")
        return redirect(url_for("dashboard"))

    return render_template("verify_otp.html", mobile=mobile)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mobile   = request.form.get("mobile",   "").strip()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(mobile=mobile, is_verified=True).first()
        if not user or not check_password_hash(user.password, password):
            flash("Invalid mobile number or password.", "danger")
            return render_template("login.html")

        session["user_id"] = user.id
        flash("Welcome back!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ─────────────────────────────────────────────
#  Routes — Dashboard & Photo Management
# ─────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    guard = login_required_redirect()
    if guard:
        return guard

    user   = current_user()
    photos = Photo.query.filter_by(user_id=user.id).order_by(Photo.uploaded_at.desc()).all()

    notifications = (
        db.session.query(ViewLog, Photo)
        .join(Photo, ViewLog.photo_id == Photo.id)
        .filter(Photo.user_id == user.id)
        .order_by(ViewLog.viewed_at.desc())
        .limit(50)
        .all()
    )

    return render_template("dashboard.html", user=user, photos=photos, notifications=notifications)


@app.route("/upload", methods=["GET", "POST"])
def upload_photo():
    guard = login_required_redirect()
    if guard:
        return guard

    user = current_user()

    if request.method == "POST":
        if "photo" not in request.files:
            flash("No file part in the form.", "danger")
            return render_template("upload.html", user=user)

        file = request.files["photo"]

        if file.filename == "":
            flash("No file selected.", "danger")
            return render_template("upload.html", user=user)

        if not allowed_file(file.filename):
            flash("Only PNG, JPG, JPEG, GIF and WEBP files are allowed.", "danger")
            return render_template("upload.html", user=user)

        ext         = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        unique_name = f"{uuid.uuid4().hex}.{ext}"
        file_content = file.read()

        # Save to disk in local dev; on Vercel we store bytes in the DB
        if not os.environ.get("VERCEL"):
            with open(os.path.join(UPLOAD_DIR, unique_name), "wb") as f:
                f.write(file_content)

        share_password = generate_share_password()
        share_token    = generate_share_token()

        photo = Photo(
            user_id       = user.id,
            filename      = unique_name,
            original_name = secure_filename(file.filename),
            share_password= share_password,
            share_token   = share_token,
            file_blob     = file_content,   # stored in DB for Vercel
        )
        db.session.add(photo)
        db.session.flush()   # get photo.id before committing

        share_url = url_for("view_shared_photo", token=share_token, _external=True)
        qr_filename, qr_blob = make_qr_code(share_url, photo.id)
        photo.qr_filename = qr_filename
        photo.qr_blob     = qr_blob
        db.session.commit()

        flash("Photo uploaded successfully!", "success")
        return redirect(url_for("photo_detail", photo_id=photo.id))

    return render_template("upload.html", user=user)


@app.route("/photo/<int:photo_id>")
def photo_detail(photo_id):
    guard = login_required_redirect()
    if guard:
        return guard

    user  = current_user()
    photo = Photo.query.filter_by(id=photo_id, user_id=user.id).first()

    if not photo:
        flash("Photo not found.", "danger")
        return redirect(url_for("dashboard"))

    view_logs = (
        ViewLog.query
        .filter_by(photo_id=photo.id)
        .order_by(ViewLog.viewed_at.desc())
        .all()
    )

    return render_template("photo_detail.html", user=user, photo=photo, view_logs=view_logs)


@app.route("/photo/<int:photo_id>/delete", methods=["POST"])
def delete_photo(photo_id):
    guard = login_required_redirect()
    if guard:
        return guard

    user  = current_user()
    photo = Photo.query.filter_by(id=photo_id, user_id=user.id).first()

    if not photo:
        flash("Photo not found.", "danger")
        return redirect(url_for("dashboard"))

    # Remove disk files only in local dev
    if not os.environ.get("VERCEL"):
        img_path = os.path.join(UPLOAD_DIR, photo.filename)
        if os.path.exists(img_path):
            os.remove(img_path)
        if photo.qr_filename:
            qr_path = os.path.join(QR_DIR, photo.qr_filename)
            if os.path.exists(qr_path):
                os.remove(qr_path)

    ViewLog.query.filter_by(photo_id=photo.id).delete()
    db.session.delete(photo)
    db.session.commit()

    flash("Photo deleted.", "info")
    return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────
#  Routes — Public Photo Viewing via QR
# ─────────────────────────────────────────────

@app.route("/view/<token>", methods=["GET", "POST"])
def view_shared_photo(token):
    photo = Photo.query.filter_by(share_token=token).first()
    if not photo:
        return render_template("error.html", message="This photo link is invalid or has been removed.")

    viewer = current_user()

    if request.method == "POST":
        entered_password = request.form.get("password", "").strip()
        viewer_mobile    = request.form.get("viewer_mobile", "").strip()

        if entered_password != photo.share_password:
            flash("Incorrect password. Please try again.", "danger")
            return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer)

        if viewer:
            viewer_mobile = viewer.mobile

        if not viewer_mobile:
            flash("Please enter your mobile number to view this photo.", "warning")
            return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer, need_mobile=True)

        view_id  = generate_view_id()
        view_log = ViewLog(
            photo_id      = photo.id,
            viewer_id     = viewer.id if viewer else None,
            view_id       = view_id,
            viewer_mobile = viewer_mobile,
        )
        db.session.add(view_log)
        db.session.commit()

        print(f"\n[NOTIFICATION] Photo '{photo.original_name}' was viewed.")
        print(f"  View ID      : {view_id}")
        print(f"  Viewer mobile: {viewer_mobile}\n")

        return render_template(
            "view_photo.html",
            token=token,
            photo=photo,
            need_password=False,
            viewer=viewer,
            view_id=view_id,
        )

    return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer)


# ─────────────────────────────────────────────
#  Routes — Media serving
# ─────────────────────────────────────────────

@app.route("/static/uploads/<filename>")
def serve_upload(filename):
    """Serve uploaded photos — from DB blob on Vercel, from disk locally."""
    photo = Photo.query.filter_by(filename=filename).first()
    if photo and photo.file_blob:
        ext  = filename.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif",  "webp": "image/webp"}.get(ext, "image/octet-stream")
        return app.response_class(photo.file_blob, mimetype=mime)
    if os.environ.get("VERCEL"):
        return "Not found", 404
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/static/qrcodes/<filename>")
@app.route("/qrcodes/<filename>")
def serve_qr(filename):
    """Serve QR codes — from DB blob on Vercel, from disk locally."""
    photo = Photo.query.filter_by(qr_filename=filename).first()
    if photo and photo.qr_blob:
        return app.response_class(photo.qr_blob, mimetype="image/png")
    if os.environ.get("VERCEL"):
        return "Not found", 404
    return send_from_directory(QR_DIR, filename)


# ─────────────────────────────────────────────
#  Bootstrap the database and run
# ─────────────────────────────────────────────

# Create tables on startup — safe to run repeatedly, no-op if tables exist.
try:
    with app.app_context():
        db.create_all()
        print("[DB] Tables verified / created OK")
except Exception as e:
    print(f"CRITICAL: Database initialisation failed: {e}")

if __name__ == "__main__":
    print("\nEyentra is starting...")
    print("Open http://127.0.0.1:5000 in your browser\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
