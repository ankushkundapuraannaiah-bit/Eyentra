"""
Eyentra - Secure Photo Sharing with QR Codes
================================================
A Flask web application that lets users register with their mobile number,
upload photos, share them via QR codes, and track who viewed them.

Author  : You
Purpose : Secure photo sharing platform
"""

import os
import io
import uuid
import random
import string
import base64
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))

import qrcode
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image
from twilio.rest import Client

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, jsonify, send_from_directory, Response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


# ─────────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-to-something-very-secret-in-production")

# SQLite database stored next to this file
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
QR_DIR     = os.path.join(BASE_DIR, "static", "qrcodes")
ANALYTICS_DIR = os.path.join(BASE_DIR, "static", "analytics")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(QR_DIR,     exist_ok=True)
os.makedirs(ANALYTICS_DIR, exist_ok=True)

database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url and database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

app.config["SQLALCHEMY_DATABASE_URI"]        = database_url or f"sqlite:///{os.path.join(BASE_DIR, 'eyentra.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"]             = 10 * 1024 * 1024   # 10 MB limit

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

db = SQLAlchemy(app)


# ─────────────────────────────────────────────
#  Database models
# ─────────────────────────────────────────────

class User(db.Model):
    """Stores registered users. Mobile number is the unique identifier."""

    __tablename__ = "users"

    id          = db.Column(db.Integer,     primary_key=True)
    mobile      = db.Column(db.String(15),  unique=True, nullable=False)
    password    = db.Column(db.String(256), nullable=False)
    is_verified = db.Column(db.Boolean,     default=False)
    created_at  = db.Column(db.DateTime,    default=datetime.utcnow)

    photos      = db.relationship("Photo",   backref="owner", lazy=True)
    view_logs   = db.relationship("ViewLog", backref="viewer", lazy=True)


class OTPRecord(db.Model):
    """Temporary OTP storage. Expires after 10 minutes."""

    __tablename__ = "otp_records"

    id         = db.Column(db.Integer,    primary_key=True)
    mobile     = db.Column(db.String(15), nullable=False)
    otp        = db.Column(db.String(6),  nullable=False)
    expires_at = db.Column(db.DateTime,   nullable=False)
    used       = db.Column(db.Boolean,    default=False)


class Photo(db.Model):
    """
    Stores uploaded photos.
    Each photo gets a unique share-password that the uploader must share
    with anyone they want to allow to view the photo via QR code.
    """

    __tablename__ = "photos"

    id             = db.Column(db.Integer,     primary_key=True)
    user_id        = db.Column(db.Integer,     db.ForeignKey("users.id"), nullable=False)
    filename       = db.Column(db.String(256), nullable=False)
    original_name  = db.Column(db.String(256), nullable=False)
    image_data     = db.Column(db.LargeBinary, nullable=True)
    image_mime     = db.Column(db.String(80),  nullable=True)
    share_password = db.Column(db.String(20),  nullable=False)   # auto-generated
    share_token    = db.Column(db.String(64),  unique=True, nullable=False)
    qr_filename    = db.Column(db.String(256), nullable=True)
    qr_data        = db.Column(db.LargeBinary, nullable=True)
    qr_mime        = db.Column(db.String(80),  nullable=True)
    uploaded_at    = db.Column(db.DateTime,    default=datetime.utcnow)

    view_logs      = db.relationship("ViewLog", backref="photo", lazy=True)


class ViewLog(db.Model):
    """
    Records every time someone views a photo using the share-password.
    A unique view-ID is generated and the uploader is notified (shown in dashboard).
    """

    __tablename__ = "view_logs"

    id         = db.Column(db.Integer,    primary_key=True)
    photo_id   = db.Column(db.Integer,   db.ForeignKey("photos.id"),  nullable=False)
    viewer_id  = db.Column(db.Integer,   db.ForeignKey("users.id"),   nullable=True)   # null = anonymous
    view_id    = db.Column(db.String(64), unique=True, nullable=False)                 # unique view event ID
    viewer_mobile = db.Column(db.String(15), nullable=True)
    viewer_type   = db.Column(db.String(40), nullable=True)
    referrer      = db.Column(db.String(512), nullable=True)
    user_agent    = db.Column(db.String(512), nullable=True)
    viewed_at  = db.Column(db.DateTime,  default=datetime.utcnow)
    notified   = db.Column(db.Boolean,   default=False)


# ─────────────────────────────────────────────
#  Helper functions
# ─────────────────────────────────────────────

def generate_otp():
    """Return a random 6-digit OTP as a string."""
    return str(random.randint(100000, 999999))


def generate_share_password(length=10):
    """
    Create a memorable share-password made of letters and digits.
    Example: 'aB3kT7mNqP'
    """
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def generate_share_token():
    """UUID-based unique token embedded in the QR code URL."""
    return str(uuid.uuid4()).replace("-", "")


def generate_view_id():
    """Short unique ID for each view event."""
    return "VW-" + str(uuid.uuid4())[:8].upper()


def ensure_database_schema():
    """Create tables and add lightweight SQLite columns added after first run."""
    db.create_all()
    if not db.engine.url.drivername.startswith("sqlite"):
        return

    existing_columns = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(view_logs)"))}
    new_view_log_columns = {
        "viewer_type": "VARCHAR(40)",
        "referrer": "VARCHAR(512)",
        "user_agent": "VARCHAR(512)",
    }
    for column_name, column_type in new_view_log_columns.items():
        if column_name not in existing_columns:
            db.session.execute(db.text(f"ALTER TABLE view_logs ADD COLUMN {column_name} {column_type}"))

    existing_photo_columns = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(photos)"))}
    new_photo_columns = {
        "image_data": "BLOB",
        "image_mime": "VARCHAR(80)",
        "qr_data": "BLOB",
        "qr_mime": "VARCHAR(80)",
    }
    for column_name, column_type in new_photo_columns.items():
        if column_name not in existing_photo_columns:
            db.session.execute(db.text(f"ALTER TABLE photos ADD COLUMN {column_name} {column_type}"))
    db.session.commit()


def infer_viewer_type(selected_type, user_agent="", referrer=""):
    """Classify a view for the analytics dashboard."""
    allowed_types = {
        "person": "People",
        "instagram": "Instagram followers",
        "facebook": "Facebook followers",
        "ai_agent": "AI agents",
        "other": "Other viewers",
    }
    if selected_type in allowed_types:
        return allowed_types[selected_type]

    text = f"{user_agent} {referrer}".lower()
    ai_markers = ["gpt", "openai", "chatgpt", "claude", "perplexity", "bot", "crawler", "spider", "agent"]
    if any(marker in text for marker in ai_markers):
        return "AI agents"
    if "instagram" in text:
        return "Instagram followers"
    if "facebook" in text or "fb.com" in text or "fb_iab" in text:
        return "Facebook followers"
    return "People"


def build_viewer_analysis(user):
    """Use pandas/numpy/seaborn to summarize and chart the user's view logs."""
    frame = viewer_analysis_frame(user)

    if frame.empty:
        return {
            "total_views": 0,
            "unique_viewers": 0,
            "category_counts": [],
            "top_photo": None,
            "chart_url": None,
        }

    category_counts = (
        frame["viewer_type"]
        .value_counts()
        .rename_axis("viewer_type")
        .reset_index(name="count")
    )
    photo_counts = frame["photo"].value_counts()
    top_photo = None
    if not photo_counts.empty:
        top_photo = {"name": photo_counts.index[0], "views": int(photo_counts.iloc[0])}

    return {
        "total_views": int(len(frame)),
        "unique_viewers": int(frame["viewer_mobile"].nunique()),
        "category_counts": category_counts.to_dict("records"),
        "top_photo": top_photo,
        "chart_url": url_for("viewer_analysis_chart"),
    }


def viewer_analysis_frame(user):
    rows = (
        db.session.query(ViewLog, Photo)
        .join(Photo, ViewLog.photo_id == Photo.id)
        .filter(Photo.user_id == user.id)
        .all()
    )
    return pd.DataFrame([{
        "photo": photo.original_name,
        "viewer_mobile": log.viewer_mobile or "Unknown",
        "viewer_type": log.viewer_type or infer_viewer_type(None, log.user_agent or "", log.referrer or ""),
        "viewed_at": log.viewed_at,
    } for log, photo in rows])


def render_viewer_analysis_chart(user):
    frame = viewer_analysis_frame(user)
    if frame.empty:
        return None

    category_counts = (
        frame["viewer_type"]
        .value_counts()
        .rename_axis("viewer_type")
        .reset_index(name="count")
    )
    sns.set_theme(style="whitegrid")
    figure = plt.figure(figsize=(8, 4.5))
    palette = sns.color_palette(["#0b7c86", "#2fbf9b", "#f26a4f", "#f2b84b", "#65736d"])
    axis = sns.barplot(data=category_counts, x="viewer_type", y="count", palette=palette[:len(category_counts)])
    axis.set_title("Viewer analysis by source", fontsize=14, weight="bold")
    axis.set_xlabel("")
    axis.set_ylabel("Views")
    axis.set_ylim(0, max(int(np.max(category_counts["count"])) + 1, 1))
    axis.tick_params(axis="x", rotation=18)
    figure.tight_layout()

    output = io.BytesIO()
    figure.savefig(output, format="png", dpi=160, bbox_inches="tight")
    plt.close(figure)
    output.seek(0)
    return output.getvalue()


def allowed_file(filename):
    """Check that the uploaded file has an accepted image extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def send_otp_simulation(mobile, otp):
    """
    Send the OTP using Twilio SMS.

    Required environment variables:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        raise RuntimeError(
            "Twilio SMS is not configured. Set TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER."
        )

    to_number = mobile if mobile.startswith("+") else f"+91{mobile}"
    client = Client(account_sid, auth_token)
    client.messages.create(
        body=f"Your Eyentra OTP is {otp}. It is valid for 10 minutes.",
        from_=from_number,
        to=to_number,
    )


def make_qr_code(share_url, photo_id):
    """
    Generate a QR code PNG for the given URL.
    Returns the filename and image bytes.
    """
    qr = qrcode.QRCode(
        version          = 1,
        error_correction = qrcode.constants.ERROR_CORRECT_H,
        box_size         = 10,
        border           = 4,
    )
    qr.add_data(share_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    qr_filename = f"qr_{photo_id}_{uuid.uuid4().hex[:8]}.png"
    output = io.BytesIO()
    img.save(output, format="PNG")
    qr_bytes = output.getvalue()

    try:
        with open(os.path.join(QR_DIR, qr_filename), "wb") as qr_file:
            qr_file.write(qr_bytes)
    except OSError:
        pass

    return qr_filename, qr_bytes, "image/png"


def create_photo_record(user, image_bytes, original_name, extension="png"):
    """Store image bytes, create the Photo row, and generate its QR code."""
    safe_original = secure_filename(original_name) or f"eyentra_capture.{extension}"
    extension = extension.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        extension = "png"

    image = Image.open(io.BytesIO(image_bytes))
    image_format = image.format
    image.verify()

    unique_name = f"{uuid.uuid4().hex}.{extension}"
    image_mime = Image.MIME.get(image_format, f"image/{extension}")

    try:
        save_path = os.path.join(UPLOAD_DIR, unique_name)
        with open(save_path, "wb") as saved_file:
            saved_file.write(image_bytes)
    except OSError:
        pass

    share_password = generate_share_password()
    share_token = generate_share_token()

    photo = Photo(
        user_id=user.id,
        filename=unique_name,
        original_name=safe_original,
        image_data=image_bytes,
        image_mime=image_mime,
        share_password=share_password,
        share_token=share_token,
    )
    db.session.add(photo)
    db.session.flush()

    share_url = url_for("view_shared_photo", token=share_token, _external=True)
    qr_filename, qr_bytes, qr_mime = make_qr_code(share_url, photo.id)
    photo.qr_filename = qr_filename
    photo.qr_data = qr_bytes
    photo.qr_mime = qr_mime
    db.session.commit()
    return photo


def current_user():
    """Return the logged-in User object, or None."""
    user_id = session.get("user_id")
    if user_id:
        return User.query.get(user_id)
    return None


def login_required_redirect(endpoint="login"):
    """Decorator-free guard: call at the top of any protected route."""
    if not session.get("user_id"):
        flash("Please log in first.", "warning")
        return redirect(url_for(endpoint))
    return None


# ─────────────────────────────────────────────
#  Routes — Authentication
# ─────────────────────────────────────────────

@app.route("/")
def index():
    user = current_user()
    return render_template("index.html", user=user)


@app.route("/register", methods=["GET", "POST"])
def register():
    """
    Step 1 of registration: collect mobile + password.
    An OTP is sent (simulated) and the user is taken to the verify page.
    """
    if request.method == "POST":
        mobile   = request.form.get("mobile",   "").strip()
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm",  "").strip()

        # Basic validation
        if not mobile or not mobile.isdigit() or len(mobile) < 10:
            flash("Please enter a valid mobile number (at least 10 digits).", "danger")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        # Check if already registered
        existing = User.query.filter_by(mobile=mobile).first()
        if existing and existing.is_verified:
            flash("This mobile number is already registered. Please log in.", "warning")
            return redirect(url_for("login"))

        # Create or reuse unverified user
        if not existing:
            hashed = generate_password_hash(password)
            new_user = User(mobile=mobile, password=hashed, is_verified=False)
            db.session.add(new_user)
            db.session.commit()
        else:
            # Update password in case they are re-registering
            existing.password   = generate_password_hash(password)
            existing.is_verified = False
            db.session.commit()

        # Create and store OTP
        otp        = generate_otp()
        otp_record = OTPRecord(
            mobile     = mobile,
            otp        = otp,
            expires_at = datetime.utcnow() + timedelta(minutes=10)
        )
        db.session.add(otp_record)
        db.session.commit()

        try:
            send_otp_simulation(mobile, otp)
        except Exception as exc:
            db.session.delete(otp_record)
            db.session.commit()
            flash(f"Could not send OTP SMS: {exc}", "danger")
            return render_template("register.html")

        # Keep mobile in session for the verify step
        session["pending_mobile"] = mobile
        flash(f"OTP sent to {mobile}.", "info")
        return redirect(url_for("verify_otp"))

    return render_template("register.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    """Verify the OTP that was sent during registration."""
    mobile = session.get("pending_mobile")
    if not mobile:
        return redirect(url_for("register"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()

        # Find the latest unused, unexpired OTP for this mobile
        record = (
            OTPRecord.query
            .filter_by(mobile=mobile, used=False)
            .filter(OTPRecord.expires_at > datetime.utcnow())
            .order_by(OTPRecord.id.desc())
            .first()
        )

        if not record:
            flash("OTP has expired. Please register again to get a new OTP.", "danger")
            return redirect(url_for("register"))

        if record.otp != entered_otp:
            flash("Incorrect OTP. Please try again.", "danger")
            return render_template("verify_otp.html", mobile=mobile)

        # Mark OTP as used and activate the user
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
    """Log in with mobile number and password."""
    if request.method == "POST":
        mobile   = request.form.get("mobile",   "").strip()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(mobile=mobile, is_verified=True).first()

        if not user or not check_password_hash(user.password, password):
            flash("Invalid mobile number or password.", "danger")
            return render_template("login.html")

        session["user_id"] = user.id
        flash(f"Welcome back!", "success")
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
    """Show the user's uploaded photos and view notifications."""
    guard = login_required_redirect()
    if guard:
        return guard

    user   = current_user()
    photos = Photo.query.filter_by(user_id=user.id).order_by(Photo.uploaded_at.desc()).all()

    # Gather view notifications (view logs for this user's photos)
    notifications = (
        db.session.query(ViewLog, Photo)
        .join(Photo, ViewLog.photo_id == Photo.id)
        .filter(Photo.user_id == user.id)
        .order_by(ViewLog.viewed_at.desc())
        .limit(50)
        .all()
    )
    viewer_analysis = build_viewer_analysis(user)

    return render_template(
        "dashboard.html",
        user=user,
        photos=photos,
        notifications=notifications,
        viewer_analysis=viewer_analysis,
    )


@app.route("/upload", methods=["GET", "POST"])
def upload_photo():
    """Upload a photo. A share-password and QR code are auto-generated."""
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

        ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        try:
            photo = create_photo_record(user, file.read(), file.filename, ext)
        except Exception:
            db.session.rollback()
            flash("That image could not be processed. Please try another file.", "danger")
            return render_template("upload.html", user=user)

        flash("Photo uploaded successfully!", "success")
        return redirect(url_for("photo_detail", photo_id=photo.id))

    return render_template("upload.html", user=user)


@app.route("/extension")
def extension_guide():
    user = current_user()
    return render_template("extension.html", user=user)


@app.route("/api/extension/session")
def extension_session():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "mobile": user.mobile})


@app.route("/api/extension/screenshot", methods=["POST"])
def extension_screenshot_upload():
    guard = login_required_redirect()
    if guard:
        return jsonify({"error": "Please log in to Eyentra first."}), 401

    data = request.get_json(silent=True) or {}
    image_data = data.get("image", "")
    filename = secure_filename(data.get("filename", "")) or "eyentra_capture.png"

    if not image_data.startswith("data:image/"):
        return jsonify({"error": "Screenshot image data is missing."}), 400

    try:
        header, encoded = image_data.split(",", 1)
        mime = header.split(";")[0].split(":")[1]
        extension = mime.split("/")[-1].replace("jpeg", "jpg")
        image_bytes = base64.b64decode(encoded)
        user = current_user()
        photo = create_photo_record(user, image_bytes, filename, extension)
    except Exception:
        db.session.rollback()
        return jsonify({"error": "The screenshot could not be saved."}), 400

    return jsonify({
        "ok": True,
        "photo_id": photo.id,
        "detail_url": url_for("photo_detail", photo_id=photo.id, _external=True),
        "qr_url": url_for("serve_qr", filename=photo.qr_filename, _external=True),
        "share_password": photo.share_password,
    })


@app.route("/photo/<int:photo_id>")
def photo_detail(photo_id):
    """Show a single photo with its QR code and share-password to the owner."""
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


@app.route("/photos/<int:photo_id>/image")
def serve_photo_image(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    user = current_user()
    allowed_photo_ids = session.get("allowed_photo_ids", [])
    if not ((user and photo.user_id == user.id) or photo.id in allowed_photo_ids):
        return Response("Forbidden", status=403)

    if photo.image_data:
        return Response(photo.image_data, mimetype=photo.image_mime or "image/png")
    return send_from_directory(UPLOAD_DIR, photo.filename)


@app.route("/analytics/viewer-chart.png")
def viewer_analysis_chart():
    guard = login_required_redirect()
    if guard:
        return guard

    user = current_user()
    chart = render_viewer_analysis_chart(user)
    if not chart:
        return Response(status=404)
    return Response(chart, mimetype="image/png")


@app.route("/photo/<int:photo_id>/delete", methods=["POST"])
def delete_photo(photo_id):
    """Delete a photo and all associated files."""
    guard = login_required_redirect()
    if guard:
        return guard

    user  = current_user()
    photo = Photo.query.filter_by(id=photo_id, user_id=user.id).first()

    if not photo:
        flash("Photo not found.", "danger")
        return redirect(url_for("dashboard"))

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
    """
    Public route embedded in the QR code.
    Visitors must enter the share-password to see the photo.
    If they are logged-in users, their mobile number is recorded.
    A unique view-ID is generated for every successful view.
    """
    photo = Photo.query.filter_by(share_token=token).first()

    if not photo:
        return render_template("error.html", message="This photo link is invalid or has been removed.")

    viewer = current_user()   # may be None if not logged in

    if request.method == "POST":
        entered_password = request.form.get("password", "").strip()
        viewer_mobile    = request.form.get("viewer_mobile", "").strip()
        selected_viewer_type = request.form.get("viewer_type", "").strip()
        user_agent = request.headers.get("User-Agent", "")[:512]
        referrer = request.headers.get("Referer", "")[:512]

        if entered_password != photo.share_password:
            flash("Incorrect password. Please try again.", "danger")
            return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer)

        # If the viewer is logged in we already know their mobile
        if viewer:
            viewer_mobile = viewer.mobile

        # If still no mobile, require it
        if not viewer_mobile:
            flash("Please enter your mobile number to view this photo.", "warning")
            return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer, need_mobile=True)

        # Record the view
        view_id  = generate_view_id()
        view_log = ViewLog(
            photo_id      = photo.id,
            viewer_id     = viewer.id if viewer else None,
            view_id       = view_id,
            viewer_mobile = viewer_mobile,
            viewer_type   = infer_viewer_type(selected_viewer_type, user_agent, referrer),
            user_agent    = user_agent,
            referrer      = referrer,
        )
        db.session.add(view_log)
        db.session.commit()
        allowed_photo_ids = session.get("allowed_photo_ids", [])
        if photo.id not in allowed_photo_ids:
            allowed_photo_ids.append(photo.id)
            session["allowed_photo_ids"] = allowed_photo_ids

        print(f"\n[NOTIFICATION] Photo '{photo.original_name}' was viewed.")
        print(f"  View ID      : {view_id}")
        print(f"  Viewer mobile: {viewer_mobile}\n")

        return render_template(
            "view_photo.html",
            token         = token,
            photo         = photo,
            need_password = False,
            viewer        = viewer,
            view_id       = view_id,
        )

    return render_template("view_photo.html", token=token, photo=None, need_password=True, viewer=viewer)


@app.route("/qrcodes/<filename>")
def serve_qr(filename):
    photo = Photo.query.filter_by(qr_filename=filename).first()
    if photo and photo.qr_data:
        return Response(photo.qr_data, mimetype=photo.qr_mime or "image/png")
    return send_from_directory(QR_DIR, filename)


# ─────────────────────────────────────────────
#  Bootstrap the database and run
# ─────────────────────────────────────────────

if os.environ.get("VERCEL") or os.environ.get("AUTO_CREATE_TABLES", "1") == "1":
    with app.app_context():
        ensure_database_schema()

if __name__ == "__main__":
    with app.app_context():
        ensure_database_schema()
        print("\nEyentra is starting...")
        print("Open http://127.0.0.1:5000 in your browser\n")

    app.run(debug=True, host="0.0.0.0", port=5000)
