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

# SQLite database stored next to this file
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
QR_DIR     = os.path.join(BASE_DIR, "static", "qrcodes")

# Create directories for local development (skipped on Vercel)
if not os.environ.get("VERCEL"):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(QR_DIR,     exist_ok=True)

# Database configuration — support PostgreSQL for production (Vercel/Neon)
database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or os.environ.get("STORAGE_URL")
if database_url:
    # SQLAlchemy requires 'postgresql://' instead of 'postgres://'
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'eyentra.db')}"

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
    share_password = db.Column(db.String(20),  nullable=False)   # auto-generated
    share_token    = db.Column(db.String(64),  unique=True, nullable=False)
    qr_filename    = db.Column(db.String(256), nullable=True)
    file_blob      = db.Column(db.LargeBinary, nullable=True)     # For production/Vercel storage
    qr_blob        = db.Column(db.LargeBinary, nullable=True)     # For production/Vercel storage
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


def allowed_file(filename):
    """Check that the uploaded file has an accepted image extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def send_otp_simulation(mobile, otp):
    """
    In a real deployment you would call an SMS gateway here (Twilio, MSG91, etc.).
    For development we just print the OTP to the terminal.
    """
    print(f"\n{'='*50}")
    print(f"  [SMS SIMULATION]  To: {mobile}   OTP: {otp}")
    print(f"{'='*50}\n")


def make_qr_code(share_url, photo_id=None):
    """
    Generate a QR code image for the given URL and save it to disk.
    Returns the filename and the binary image data.
    """
    qr = qrcode.QRCode(
        version          = 1,
        error_correction = qrcode.constants.ERROR_CORRECT_H,
        box_size         = 10,
        border           = 4,
    )
    qr.add_data(share_url)
    qr.make(fit=True)

    img          = qr.make_image(fill_color="black", back_color="white")
    
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    qr_blob = img_io.getvalue()
    
    qr_filename = f"qr_{photo_id}_{uuid.uuid4().hex[:8]}.png" if photo_id else f"qr_{uuid.uuid4().hex[:8]}.png"
    if not os.environ.get("VERCEL"):
        img.save(os.path.join(QR_DIR, qr_filename))
        
    return qr_filename, qr_blob


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

@app.route("/health")
def health():
    """Health check route for Vercel/Production verification."""
    try:
        User.query.limit(1).all()
        return jsonify({"status": "healthy", "database": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


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

        send_otp_simulation(mobile, otp)

        # Keep mobile in session for the verify step
        session["pending_mobile"] = mobile
        flash(f"OTP sent to {mobile}. (Check the terminal for development OTP)", "info")
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

    return render_template("dashboard.html", user=user, photos=photos, notifications=notifications)


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

        # Save the file with a unique name
        ext            = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        unique_name    = f"{uuid.uuid4().hex}.{ext}"
        
        file_content   = file.read()
        if not os.environ.get("VERCEL"):
            save_path = os.path.join(UPLOAD_DIR, unique_name)
            with open(save_path, "wb") as f:
                f.write(file_content)

        share_password = generate_share_password()
        share_token    = generate_share_token()

        photo = Photo(
            user_id       = user.id,
            filename      = unique_name,
            original_name = secure_filename(file.filename),
            share_password= share_password,
            share_token   = share_token,
            file_blob     = file_content
        )
        db.session.add(photo)
        db.session.flush()   # get photo.id before committing

        # Build the public share URL and generate QR code
        share_url      = url_for("view_shared_photo", token=share_token, _external=True)
        qr_filename, qr_blob = make_qr_code(share_url, photo.id)
        photo.qr_filename = qr_filename
        photo.qr_blob     = qr_blob
        db.session.commit()

        flash("Photo uploaded successfully!", "success")
        return redirect(url_for("photo_detail", photo_id=photo.id))

    return render_template("upload.html", user=user)


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

    # Remove the image file
    img_path = os.path.join(UPLOAD_DIR, photo.filename)
    if os.path.exists(img_path):
        os.remove(img_path)

    # Remove the QR code file
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
        )
        db.session.add(view_log)
        db.session.commit()

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


# ─────────────────────────────────────────────
#  Routes — Media Serving (Database Fallback for Production)
# ─────────────────────────────────────────────

@app.route("/static/uploads/<filename>")
def serve_upload_compat(filename):
    """Serve uploaded photos from DB if disk is missing (for Vercel)."""
    photo = Photo.query.filter_by(filename=filename).first()
    if photo and photo.file_blob:
        return app.response_class(photo.file_blob, mimetype='image/png')
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/static/qrcodes/<filename>")
@app.route("/qrcodes/<filename>")
def serve_qr_compat(filename):
    """Serve QR codes from DB if disk is missing (for Vercel)."""
    photo = Photo.query.filter_by(qr_filename=filename).first()
    if photo and photo.qr_blob:
        return app.response_class(photo.qr_blob, mimetype='image/png')
    return send_from_directory(QR_DIR, filename)


# ─────────────────────────────────────────────
#  Bootstrap the database and run
# ─────────────────────────────────────────────

# Ensure tables are created on startup (needed for Vercel/Production)
try:
    with app.app_context():
        db.create_all()
except Exception as e:
    print(f"Error initializing database: {e}")


if __name__ == "__main__":
    print("\nEyentra is starting...")
    print("Open http://127.0.0.1:5000 in your browser\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
