from twilio.rest import Client
import os
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename
import uuid
from flask import (
    Flask, render_template, redirect, url_for,
    request, flash, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

app = Flask(__name__, instance_relative_config=True)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "dev-only-change-this-key"
)

# SQLite database path - use absolute path for deployment safety
_database_url = os.getenv("DATABASE_URL", "sqlite:///medicine.db")

# Only normalize relative sqlite paths to absolute instance paths.
# Keep :memory: and absolute paths (sqlite:////...) untouched.
if (
    _database_url.startswith("sqlite:///")
    and not _database_url.startswith("sqlite:////")
    and ":memory:" not in _database_url
):
    _db_rel = _database_url.replace("sqlite:///", "", 1)
    _db_path = os.path.join(app.instance_path, _db_rel)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_db_path}"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = _database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Medical records upload configuration - ensure writable absolute path
app.config["MEDICAL_RECORD_UPLOAD_FOLDER"] = os.path.join(
    app.instance_path, "medical_records"
)

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ALLOWED_MEDICAL_RECORD_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "webp"
}

os.makedirs(app.config["MEDICAL_RECORD_UPLOAD_FOLDER"], exist_ok=True)

# VAPID keys - support both file path and direct key in env
_vapid_private_key = os.getenv("VAPID_PRIVATE_KEY", "")
_vapid_private_key_path = os.getenv("VAPID_PRIVATE_KEY_PATH", "")

if _vapid_private_key:
    # Write the key to a file in instance folder if provided directly
    _key_file = os.path.join(app.instance_path, "vapid_private.pem")
    try:
        with open(_key_file, "w") as f:
            f.write(_vapid_private_key)
        app.config["VAPID_PRIVATE_KEY_PATH"] = _key_file
    except OSError:
        app.config["VAPID_PRIVATE_KEY_PATH"] = _vapid_private_key_path
else:
    # Use path relative to app root if not absolute
    if _vapid_private_key_path and not os.path.isabs(_vapid_private_key_path):
        app.config["VAPID_PRIVATE_KEY_PATH"] = os.path.join(
            app.root_path, _vapid_private_key_path
        )
    else:
        app.config["VAPID_PRIVATE_KEY_PATH"] = _vapid_private_key_path

app.config["VAPID_PUBLIC_KEY"] = os.getenv("VAPID_PUBLIC_KEY", "")
app.config["VAPID_CLAIMS_EMAIL"] = os.getenv(
    "VAPID_CLAIMS_EMAIL", "mailto:your-email@example.com"
)

os.makedirs(app.instance_path, exist_ok=True)

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."

MISSED_DOSE_GRACE_MINUTES = 15


# =========================================================
# TIME FORMATTER
# =========================================================

@app.template_filter("time12")
def time12(value):
    if not value:
        return "Not set"

    try:
        return datetime.strptime(
            str(value), "%H:%M"
        ).strftime("%I:%M %p")
    except ValueError:
        return str(value)


# =========================================================
# DATABASE MODELS
# =========================================================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(
        db.String(120), unique=True, nullable=False, index=True
    )
    password_hash = db.Column(db.String(255), nullable=False)
    age = db.Column(db.Integer)
    phone = db.Column(db.String(20))
    emergency_contact = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    medicines = db.relationship(
        "Medicine",
        backref="patient",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    dosage = db.Column(db.String(100), nullable=False)

    # Retained for compatibility with the existing database.
    scheduled_time = db.Column(db.String(5), nullable=False)

    instructions = db.Column(db.String(300))
    stock_quantity = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, default=True, nullable=False)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    dose_logs = db.relationship(
        "DoseLog",
        backref="medicine",
        lazy=True,
        cascade="all, delete-orphan",
    )

    schedules = db.relationship(
        "MedicineSchedule",
        backref="medicine",
        lazy=True,
        cascade="all, delete-orphan",
    )


class DoseLog(db.Model):
    __tablename__ = "dose_log"

    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(
        db.Integer,
        db.ForeignKey("medicine.id"),
        nullable=False,
    )
    scheduled_date = db.Column(db.String(10), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    response_time = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint(
            "medicine_id",
            "scheduled_date",
            name="unique_medicine_daily_log",
        ),
    )


class MedicineSchedule(db.Model):
    __tablename__ = "medicine_schedule"

    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(
        db.Integer,
        db.ForeignKey("medicine.id"),
        nullable=False,
        index=True,
    )
    scheduled_time = db.Column(db.String(5), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    events = db.relationship(
        "DoseEvent",
        backref="schedule",
        lazy=True,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "medicine_id",
            "scheduled_time",
            name="unique_medicine_schedule_time",
        ),
    )


class DoseEvent(db.Model):
    __tablename__ = "dose_event"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(
        db.Integer,
        db.ForeignKey("medicine_schedule.id"),
        nullable=False,
        index=True,
    )
    scheduled_date = db.Column(db.String(10), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    response_time = db.Column(db.DateTime)
    legacy_log_id = db.Column(db.Integer, unique=True, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "schedule_id",
            "scheduled_date",
            name="unique_schedule_daily_event",
        ),
    )

class MedicalRecord(db.Model):

    __tablename__ = "medical_record"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True
    )

    title = db.Column(db.String(150), nullable=False)

    record_type = db.Column(db.String(30), nullable=False)

    doctor_name = db.Column(db.String(120))

    hospital_name = db.Column(db.String(150))

    consultation_date = db.Column(db.String(10), nullable=False, index=True)

    next_consultation_date = db.Column(db.String(10))

    notes = db.Column(db.Text)

    original_filename = db.Column(db.String(255), nullable=False)

    stored_filename = db.Column(db.String(255), nullable=False, unique=True)

    uploaded_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

class PushSubscription(db.Model):
    __tablename__ = "push_subscription"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PushNotificationLog(db.Model):
    __tablename__ = "push_notification_log"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(
        db.Integer,
        db.ForeignKey("medicine_schedule.id"),
        nullable=False,
        index=True,
    )
    scheduled_date = db.Column(db.String(10), nullable=False)
    sent_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )
    # =========================================================
# LOGIN MANAGEMENT
# =========================================================

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (ValueError, TypeError):
        return None


# =========================================================
# DATABASE MIGRATION / OLD HISTORY
# =========================================================

def ensure_schedules_and_migrate_legacy_logs():

    for medicine in Medicine.query.all():

        existing = MedicineSchedule.query.filter_by(
            medicine_id=medicine.id
        ).first()

        if existing is None:
            db.session.add(
                MedicineSchedule(
                    medicine_id=medicine.id,
                    scheduled_time=medicine.scheduled_time,
                    active=medicine.active,
                )
            )

    db.session.flush()

    for old_log in DoseLog.query.all():

        if DoseEvent.query.filter_by(
            legacy_log_id=old_log.id
        ).first():
            continue

        medicine = db.session.get(
            Medicine,
            old_log.medicine_id
        )

        if medicine is None:
            continue

        schedule = MedicineSchedule.query.filter_by(
            medicine_id=medicine.id,
            scheduled_time=medicine.scheduled_time,
        ).first()

        if schedule is None:
            continue

        existing_event = DoseEvent.query.filter_by(
            schedule_id=schedule.id,
            scheduled_date=old_log.scheduled_date,
        ).first()

        if existing_event:

            if existing_event.legacy_log_id is None:
                existing_event.legacy_log_id = old_log.id

            continue

        db.session.add(
            DoseEvent(
                schedule_id=schedule.id,
                scheduled_date=old_log.scheduled_date,
                status=old_log.status,
                response_time=old_log.response_time,
                legacy_log_id=old_log.id,
            )
        )

    db.session.commit()


# =========================================================
# MISSED DOSE DETECTION + EMERGENCY CONTACT ALERT
# =========================================================

def send_missed_dose_alert(user, medicine, scheduled_time):

    """
    Sends a push notification to the patient's registered devices
    and an SMS to the emergency contact when a dose is missed.
    """

    formatted_time = time12(scheduled_time)

    # -----------------------------------------------------
    # 1. PUSH NOTIFICATION TO PATIENT
    # -----------------------------------------------------

    subscriptions = PushSubscription.query.filter_by(
        user_id=user.id
    ).all()

    for subscription in subscriptions:

        try:
            send_push_notification(
                subscription,
                "Medicine Dose Missed",
                (
                    f"You missed {medicine.name} ({medicine.dosage}), "
                    f"scheduled for {formatted_time}. "
                    "Please check your medicine schedule."
                ),
            )

        except Exception:
            app.logger.exception(
                "Push alert failed for user %s",
                user.id
            )

    if not subscriptions:
        app.logger.warning(
            "No push subscriptions found for user %s",
            user.id
        )

    # -----------------------------------------------------
    # 2. SMS TO EMERGENCY CONTACT
    # -----------------------------------------------------

    emergency_number = (
        getattr(user, "emergency_contact", None) or ""
    ).strip()

    if not emergency_number:

        app.logger.warning(
            "Emergency contact number missing for user %s",
            user.id
        )

        return

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not all([account_sid, auth_token, twilio_number]):

        app.logger.warning(
            "Twilio is not configured; emergency SMS was not sent."
        )

        return

    message_body = (
        f"MediCare AI Alert: A medicine dose has been missed. "
        f"Patient: {user.name}. "
        f"Medicine: {medicine.name} ({medicine.dosage}). "
        f"Scheduled time: {formatted_time}. "
        f"Please contact the patient and check on them."
    )

    try:

        client = Client(account_sid, auth_token)

        message = client.messages.create(
            body=message_body,
            from_=twilio_number,
            to=emergency_number,
        )

        app.logger.info(
            "Emergency SMS submitted successfully. "
            "Message SID: %s",
            message.sid,
        )

    except Exception:

        app.logger.exception(
            "Failed to send emergency SMS for user %s",
            user.id
        )


# =========================================================
# MARK OVERDUE DOSES
# =========================================================

def mark_overdue_doses(user_id):

    """
    Marks a dose as missed 15 minutes after its scheduled time.
    Sends alerts only when the dose first becomes missed.
    """

    now = datetime.now()
    today_string = now.date().isoformat()

    schedules = (
        MedicineSchedule.query
        .join(Medicine)
        .filter(
            Medicine.user_id == user_id,
            Medicine.active.is_(True),
            MedicineSchedule.active.is_(True),
        )
        .all()
    )

    changed = False
    newly_missed = []

    for schedule in schedules:

        try:
            scheduled_time = datetime.strptime(
                schedule.scheduled_time,
                "%H:%M"
            ).time()

        except (ValueError, TypeError):
            continue

        overdue_at = datetime.combine(
            now.date(),
            scheduled_time
        ) + timedelta(
            minutes=MISSED_DOSE_GRACE_MINUTES
        )

        if now < overdue_at:
            continue

        event = DoseEvent.query.filter_by(
            schedule_id=schedule.id,
            scheduled_date=today_string,
        ).first()

        if event is None:

            event = DoseEvent(
                schedule_id=schedule.id,
                scheduled_date=today_string,
                status="missed",
                response_time=now,
            )

            db.session.add(event)

            changed = True
            newly_missed.append(schedule)

        elif event.status == "pending":

            event.status = "missed"
            event.response_time = now

            changed = True
            newly_missed.append(schedule)

        # Taken, snoozed, or already-missed events
        # are not changed or alerted again.

    if changed:

        try:
            db.session.commit()

        except Exception:

            db.session.rollback()

            app.logger.exception(
                "Could not save missed-dose events for user %s",
                user_id
            )

            return

    # Send alerts after database changes are saved.

    for schedule in newly_missed:

        try:
            send_missed_dose_alert(
                schedule.medicine.patient,
                schedule.medicine,
                schedule.scheduled_time,
            )

        except Exception:

            app.logger.exception(
                "Failed to send missed-dose alert for schedule %s",
                schedule.id
            )
# =========================================================
# PUSH NOTIFICATIONS
# =========================================================

@app.route("/push/public-key")
@login_required
def push_public_key():
    public_key = app.config["VAPID_PUBLIC_KEY"]

    if not public_key:
        return jsonify({
            "error": "VAPID public key is not configured"
        }), 500

    return jsonify({"publicKey": public_key})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    keys = data.get("keys") or {}

    endpoint = data.get("endpoint")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not all([endpoint, p256dh, auth]):
        return jsonify({"error": "Invalid push subscription"}), 400

    subscription = PushSubscription.query.filter_by(
        endpoint=endpoint
    ).first()

    if subscription is None:
        subscription = PushSubscription(
            user_id=current_user.id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )
        db.session.add(subscription)
    else:
        subscription.user_id = current_user.id
        subscription.p256dh = p256dh
        subscription.auth = auth

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not save push subscription")
        return jsonify({"error": "Could not save subscription"}), 500

    return jsonify({"success": True}), 201


def send_push_notification(subscription, title, body):
    private_key_path = Path(
        app.config["VAPID_PRIVATE_KEY_PATH"]
    )

    if not private_key_path.is_absolute():
        private_key_path = Path(app.root_path) / private_key_path

    if not private_key_path.exists():
        app.logger.error(
            "VAPID private key file not found: %s",
            private_key_path,
        )
        return False

    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.p256dh,
            "auth": subscription.auth,
        },
    }

    payload = {
        "title": title,
        "body": body,
        "url": "/dashboard",
    }

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=str(private_key_path),
            vapid_claims={
                "sub": app.config["VAPID_CLAIMS_EMAIL"]
            },
        )
        return True

    except WebPushException as exc:
        app.logger.warning("Push notification failed: %s", exc)

        response = getattr(exc, "response", None)

        if response is not None and response.status_code in (404, 410):
            db.session.delete(subscription)
            db.session.commit()

        return False

    except Exception:
        app.logger.exception("Unexpected push notification error")
        return False


# =========================================================
# MEDICINE REMINDERS
# Sends an early reminder 10 minutes before the dose.
# =========================================================

def check_and_send_medicine_reminders():
    with app.app_context():
        now = datetime.now()
        today = now.date().isoformat()

        schedules = (
            MedicineSchedule.query
            .join(Medicine)
            .filter(
                Medicine.active.is_(True),
                MedicineSchedule.active.is_(True),
            )
            .all()
        )

        for schedule in schedules:
            try:
                scheduled_time = datetime.strptime(
                    schedule.scheduled_time, "%H:%M"
                ).time()
            except (ValueError, TypeError):
                app.logger.warning(
                    "Invalid scheduled time for schedule %s",
                    schedule.id,
                )
                continue

            dose_datetime = datetime.combine(
                now.date(), scheduled_time
            )

            # Skip doses that are more than 10 minutes in the future
            # or already past the dose window (more than 60 min past).
            if dose_datetime > now + timedelta(minutes=10):
                continue
            if dose_datetime < now - timedelta(minutes=60):
                continue

            event = DoseEvent.query.filter_by(
                schedule_id=schedule.id,
                scheduled_date=today,
            ).first()

            if event and event.status == "taken":
                continue

            existing_log = PushNotificationLog.query.filter_by(
                schedule_id=schedule.id,
                scheduled_date=today,
            ).first()

            if existing_log:
                continue

            medicine = schedule.medicine

            subscriptions = PushSubscription.query.filter_by(
                user_id=medicine.user_id
            ).all()

            if not subscriptions:
                app.logger.info(
                    "No push subscription for user %s",
                    medicine.user_id,
                )
                continue

            formatted_time = dose_datetime.strftime("%I:%M %p")
            sent = False

            for subscription in subscriptions:
                success = send_push_notification(
                    subscription,
                    "Medicine Reminder — 10 Minutes Left",
                    (
                        f"Your {medicine.name} ({medicine.dosage}) "
                        f"is scheduled for {formatted_time}. "
                        "Please get ready to take your medicine."
                    ),
                )

                if success:
                    sent = True

            if sent:
                db.session.add(
                    PushNotificationLog(
                        schedule_id=schedule.id,
                        scheduled_date=today,
                    )
                )

                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    app.logger.exception(
                        "Could not save early reminder log"
                    )


# =========================================================
# AUTOMATIC MISSED-DOSE CHECKING
# =========================================================

def check_all_users_for_missed_doses():
    with app.app_context():
        try:
            user_ids = [
                user_id
                for (user_id,) in db.session.query(User.id).all()
            ]

            for user_id in user_ids:
                try:
                    mark_overdue_doses(user_id)
                except Exception:
                    db.session.rollback()
                    app.logger.exception(
                        "Missed-dose check failed for user %s",
                        user_id,
                    )

        except Exception:
            db.session.rollback()
            app.logger.exception(
                "Automatic missed-dose checking failed"
            )
# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# =========================================================
# REGISTRATION
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please fill in all required fields.", "error")
            return render_template("register.html")

        if len(name) > 100 or len(email) > 120:
            flash("Name or email is too long.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash(
                "Password must contain at least 8 characters.",
                "error",
            )
            return render_template("register.html")

        existing_user = db.session.execute(
            db.select(User).filter_by(email=email)
        ).scalar_one_or_none()

        if existing_user:
            flash(
                "An account with this email already exists.",
                "error",
            )
            return render_template("register.html")

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Registration failed")
            flash("Registration failed. Please try again.", "error")
            return render_template("register.html")

        login_user(user)
        flash("Account created successfully!", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = db.session.execute(
            db.select(User).filter_by(email=email)
        ).scalar_one_or_none()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


# =========================================================
# STREAKS & BADGES
# =========================================================

def get_streak_data(user_id):
    today = date.today()

    schedules = (
        MedicineSchedule.query
        .join(Medicine)
        .filter(
            Medicine.user_id == user_id,
            Medicine.active.is_(True),
            MedicineSchedule.active.is_(True),
        )
        .all()
    )

    # No active medicine schedules = no streak.
    if not schedules:
        return {
            "current_streak": 0,
            "perfect_today": False,
            "earned_badges": [],
            "next_badge": 3,
            "badge_progress": 0,
        }

    schedule_ids = [schedule.id for schedule in schedules]

    events = DoseEvent.query.filter(
        DoseEvent.schedule_id.in_(schedule_ids),
        DoseEvent.scheduled_date >= (
            today - timedelta(days=365)
        ).isoformat(),
        DoseEvent.scheduled_date <= today.isoformat(),
    ).all()

    event_map = {
        (event.schedule_id, event.scheduled_date): event.status
        for event in events
    }

    def is_perfect_day(day):
        day_string = day.isoformat()

        return all(
            event_map.get((schedule_id, day_string)) == "taken"
            for schedule_id in schedule_ids
        )

    perfect_today = is_perfect_day(today)

    # If today's doses are not all completed yet, calculate
    # the existing streak starting from yesterday.
    check_day = today if perfect_today else today - timedelta(days=1)

    current_streak = 0

    for _ in range(365):
        if not is_perfect_day(check_day):
            break

        current_streak += 1
        check_day -= timedelta(days=1)

    badge_levels = [3, 7, 15, 30]

    earned_badges = [
        level for level in badge_levels
        if current_streak >= level
    ]

    next_badge = next(
        (level for level in badge_levels if current_streak < level),
        None,
    )

    if next_badge is None:
        badge_progress = 100
    else:
        previous_badge = max(
            [level for level in badge_levels if level < next_badge],
            default=0,
        )
        badge_progress = round(
            (current_streak - previous_badge)
            / (next_badge - previous_badge) * 100
        )

    return {
        "current_streak": current_streak,
        "perfect_today": perfect_today,
        "earned_badges": earned_badges,
        "next_badge": next_badge,
        "badge_progress": badge_progress,
    }


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard")
@login_required
def dashboard():
    mark_overdue_doses(current_user.id)
    today = date.today().isoformat()

    schedules = (
        MedicineSchedule.query
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            Medicine.active.is_(True),
            MedicineSchedule.active.is_(True),
        )
        .order_by(MedicineSchedule.scheduled_time)
        .all()
    )

    dose_items = []

    for schedule in schedules:
        event = DoseEvent.query.filter_by(
            schedule_id=schedule.id,
            scheduled_date=today,
        ).first()

        dose_items.append({
            "schedule": schedule,
            "medicine": schedule.medicine,
            "event": event,
            "status": event.status if event else "pending",
        })

    taken = sum(item["status"] == "taken" for item in dose_items)
    missed = sum(item["status"] == "missed" for item in dose_items)
    snoozed = sum(item["status"] == "snoozed" for item in dose_items)

    total = len(dose_items)
    adherence = round(taken / total * 100) if total else 0

    # -----------------------------------------------------
    # STREAKS & BADGES
    # -----------------------------------------------------

    streak_data = get_streak_data(current_user.id)

    medicines = Medicine.query.filter_by(
        user_id=current_user.id,
        active=True,
    ).all()

    return render_template(
        "dashboard.html",
        medicines=medicines,
        dose_items=dose_items,
        taken=taken,
        missed=missed,
        snoozed=snoozed,
        total=total,
        adherence=adherence,

        # Streak and badge information
        **streak_data,

        log_by_medicine={},
    )


# =========================================================
# PROFILE
# =========================================================

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        age_text = request.form.get("age", "").strip()

        if age_text:
            try:
                age = int(age_text)
                if not 1 <= age <= 120:
                    raise ValueError
                current_user.age = age
            except ValueError:
                flash("Please enter a valid age.", "error")
                return render_template("profile.html")

        current_user.phone = (
            request.form.get("phone", "").strip() or None
        )
        current_user.emergency_contact = (
            request.form.get("emergency_contact", "").strip() or None
        )

        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html")
# =========================================================
# MEDICINE MANAGEMENT
# =========================================================

@app.route("/medicines", methods=["GET", "POST"])
@login_required
def medicines():
    mark_overdue_doses(current_user.id)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dosage = request.form.get("dosage", "").strip()
        times = request.form.getlist("scheduled_times")

        if not times:
            old_time = request.form.get("scheduled_time", "").strip()
            if old_time:
                times = [old_time]

        times = sorted(set(t.strip() for t in times if t.strip()))
        instructions = request.form.get("instructions", "").strip()

        if not name or not dosage or not times:
            flash(
                "Enter the medicine name, dosage and at least one time.",
                "error",
            )
            return redirect(url_for("medicines"))

        if len(name) > 120 or len(dosage) > 100:
            flash("Medicine name or dosage is too long.", "error")
            return redirect(url_for("medicines"))

        if len(instructions) > 300:
            flash("Instructions must be 300 characters or fewer.", "error")
            return redirect(url_for("medicines"))

        for scheduled_time in times:
            try:
                datetime.strptime(scheduled_time, "%H:%M")
            except ValueError:
                flash("One of the selected times is invalid.", "error")
                return redirect(url_for("medicines"))

        medicine = Medicine(
            name=name,
            dosage=dosage,
            scheduled_time=times[0],
            instructions=instructions or None,
            user_id=current_user.id,
        )

        db.session.add(medicine)
        db.session.flush()

        for scheduled_time in times:
            db.session.add(
                MedicineSchedule(
                    medicine_id=medicine.id,
                    scheduled_time=scheduled_time,
                    active=True,
                )
            )

        db.session.commit()
        flash(
            f"Medicine added with {len(times)} scheduled time(s).",
            "success",
        )
        return redirect(url_for("medicines"))

    medicine_list = Medicine.query.filter_by(
        user_id=current_user.id,
        active=True,
    ).order_by(Medicine.scheduled_time).all()

    for medicine in medicine_list:
        if not MedicineSchedule.query.filter_by(
            medicine_id=medicine.id
        ).first():
            db.session.add(
                MedicineSchedule(
                    medicine_id=medicine.id,
                    scheduled_time=medicine.scheduled_time,
                    active=True,
                )
            )

    db.session.commit()

    for medicine in medicine_list:
        medicine.schedule_list = MedicineSchedule.query.filter_by(
            medicine_id=medicine.id,
            active=True,
        ).order_by(MedicineSchedule.scheduled_time).all()

        medicine.display_time = time12(medicine.scheduled_time)

    return render_template(
        "medicines.html",
        medicines=medicine_list,
        log_by_medicine={},
    )


@app.route("/medicines/delete/<int:medicine_id>", methods=["POST"])
@login_required
def delete_medicine(medicine_id):
    medicine = db.session.get(Medicine, medicine_id)

    if medicine is None or medicine.user_id != current_user.id:
        flash("Medicine not found.", "error")
        return redirect(url_for("medicines"))

    medicine.active = False

    for schedule in medicine.schedules:
        schedule.active = False

    db.session.commit()
    flash("Medicine removed from your active schedule.", "success")
    return redirect(url_for("medicines"))


@app.route("/medicines/<int:medicine_id>/refill", methods=["POST"])
@login_required
def refill_medicine(medicine_id):
    medicine = db.session.get(Medicine, medicine_id)

    if medicine is None or medicine.user_id != current_user.id:
        flash("Medicine not found.", "error")
        return redirect(url_for("medicines"))

    try:
        amount = int(request.form.get("refill_amount", "0"))

        if amount <= 0 or amount > 10000:
            raise ValueError

    except (TypeError, ValueError):
        flash("Enter a valid refill quantity.", "error")
        return redirect(url_for("medicines"))

    medicine.stock_quantity = (medicine.stock_quantity or 0) + amount

    try:
        db.session.commit()
        flash(
            f"Added {amount} units to {medicine.name} stock.",
            "success",
        )
    except Exception:
        db.session.rollback()
        app.logger.exception("Medicine refill failed")
        flash("Could not update stock.", "error")

    return redirect(url_for("medicines"))


# =========================================================
# DOSE RESPONSE
# =========================================================

def record_dose_response(schedule_id):
    schedule = db.session.get(MedicineSchedule, schedule_id)

    if (
        schedule is None
        or not schedule.active
        or not schedule.medicine.active
        or schedule.medicine.user_id != current_user.id
    ):
        flash("Scheduled dose not found.", "error")
        return redirect(url_for("dashboard"))

    status = request.form.get("status", "").strip().lower()

    if status not in {"taken", "missed", "snoozed"}:
        flash("Invalid medicine response.", "error")
        return redirect(url_for("dashboard"))

    today = date.today().isoformat()

    event = DoseEvent.query.filter_by(
        schedule_id=schedule.id,
        scheduled_date=today
    ).first()

    # A dose already marked taken/missed cannot be recorded again.
    if event and event.status in {"taken", "missed"}:
        flash("This dose has already been recorded.", "warning")
        return redirect(url_for("dashboard"))

    if status == "taken" and (schedule.medicine.stock_quantity or 0) <= 0:
        flash("Medicine stock is empty. Please refill it first.", "error")
        return redirect(url_for("dashboard"))

    if event is None:
        event = DoseEvent(
            schedule_id=schedule.id,
            scheduled_date=today,
            status=status,
            response_time=datetime.now(),
        )
        db.session.add(event)
    else:
        event.status = status
        event.response_time = datetime.now()

    if status == "taken":
        schedule.medicine.stock_quantity -= 1

    try:
        db.session.commit()
        flash("Dose response recorded.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not record dose response")
        flash("Could not save dose response. Please try again.", "error")

    return redirect(url_for("dashboard"))


@app.route("/dose-response/<int:schedule_id>", methods=["POST"])
@login_required
def dose_response(schedule_id):
    return record_dose_response(schedule_id)


@app.route("/medicine-response/<int:medicine_id>", methods=["POST"])
@login_required
def medicine_response(medicine_id):
    medicine = db.session.get(Medicine, medicine_id)

    if (
        medicine is None
        or medicine.user_id != current_user.id
        or not medicine.active
    ):
        flash("Active medicine not found.", "error")
        return redirect(url_for("dashboard"))

    schedule = MedicineSchedule.query.filter_by(
        medicine_id=medicine.id,
        active=True,
    ).order_by(MedicineSchedule.scheduled_time).first()

    if schedule is None:
        flash("No active schedule found.", "error")
        return redirect(url_for("dashboard"))

    return record_dose_response(schedule.id)
# =========================================================
# ANALYTICS
# =========================================================

@app.route("/analytics")
@login_required
def analytics():
    mark_overdue_doses(current_user.id)

    today = date.today()
    start_date = today - timedelta(days=6)

    weekly_logs = (
        DoseEvent.query
        .join(MedicineSchedule)
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            DoseEvent.scheduled_date >= start_date.isoformat(),
            DoseEvent.scheduled_date <= today.isoformat(),
        )
        .all()
    )

    weekly_labels = []
    weekly_taken = []
    weekly_missed = []
    weekly_snoozed = []

    for offset in range(7):
        current_date = start_date + timedelta(days=offset)
        date_string = current_date.isoformat()

        day_logs = [
            log for log in weekly_logs
            if log.scheduled_date == date_string
        ]

        weekly_labels.append(current_date.strftime("%d %b"))
        weekly_taken.append(sum(log.status == "taken" for log in day_logs))
        weekly_missed.append(sum(log.status == "missed" for log in day_logs))
        weekly_snoozed.append(sum(log.status == "snoozed" for log in day_logs))

    taken_count = sum(log.status == "taken" for log in weekly_logs)
    missed_count = sum(log.status == "missed" for log in weekly_logs)
    snoozed_count = sum(log.status == "snoozed" for log in weekly_logs)

    total_responses = taken_count + missed_count + snoozed_count

    adherence = (
        round(taken_count / total_responses * 100)
        if total_responses else 0
    )

    medicines = Medicine.query.filter_by(
        user_id=current_user.id
    ).all()

    medicine_stats = []

    for medicine in medicines:
        medicine_logs = [
            log for log in weekly_logs
            if log.schedule.medicine_id == medicine.id
        ]

        medicine_stats.append({
            "name": medicine.name,
            "taken": sum(log.status == "taken" for log in medicine_logs),
            "missed": sum(log.status == "missed" for log in medicine_logs),
            "total": len(medicine_logs),
        })

    medicine_stats.sort(
        key=lambda item: item["missed"],
        reverse=True,
    )

    month_start = today.replace(day=1).isoformat()

    month_logs = (
        DoseEvent.query
        .join(MedicineSchedule)
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            DoseEvent.scheduled_date >= month_start,
            DoseEvent.scheduled_date <= today.isoformat(),
        )
        .all()
    )

    month_taken = sum(log.status == "taken" for log in month_logs)
    month_missed = sum(log.status == "missed" for log in month_logs)
    month_snoozed = sum(log.status == "snoozed" for log in month_logs)

    month_total = month_taken + month_missed + month_snoozed

    monthly_adherence = (
        round(month_taken / month_total * 100)
        if month_total else 0
    )

    return render_template(
        "analytics.html",
        weekly_labels=weekly_labels,
        weekly_taken=weekly_taken,
        weekly_missed=weekly_missed,
        weekly_snoozed=weekly_snoozed,
        taken_count=taken_count,
        missed_count=missed_count,
        snoozed_count=snoozed_count,
        total_responses=total_responses,
        adherence=adherence,
        medicine_stats=medicine_stats,
        month_taken=month_taken,
        month_missed=month_missed,
        month_snoozed=month_snoozed,
        month_total=month_total,
        monthly_adherence=monthly_adherence,
    )

# =========================================================
# MEDICINE HISTORY
# =========================================================

@app.route("/history")
@login_required
def medicine_history():
    mark_overdue_doses(current_user.id)

    selected_date = request.args.get("date", "").strip()
    selected_status = request.args.get("status", "").strip().lower()

    # Validate selected date
    if selected_date:
        try:
            selected_date = datetime.strptime(
                selected_date, "%Y-%m-%d"
            ).date().isoformat()
        except ValueError:
            selected_date = ""
            flash("Invalid date selected.", "error")

    # Validate status
    valid_statuses = {"taken", "missed", "snoozed", "pending"}

    if selected_status not in valid_statuses:
        selected_status = ""

    # Get current user's medicine history
    query = (
        DoseEvent.query
        .join(MedicineSchedule)
        .join(Medicine)
        .filter(Medicine.user_id == current_user.id)
    )

    # Apply date filter
    if selected_date:
        query = query.filter(
            DoseEvent.scheduled_date == selected_date
        )

    # Apply status filter
    if selected_status:
        query = query.filter(
            DoseEvent.status == selected_status
        )

    history = query.order_by(
        DoseEvent.scheduled_date.desc(),
        DoseEvent.response_time.desc(),
        DoseEvent.id.desc()
    ).all()

    taken_count = sum(log.status == "taken" for log in history)
    missed_count = sum(log.status == "missed" for log in history)
    snoozed_count = sum(log.status == "snoozed" for log in history)

    return render_template(
        "history.html",
        history=history,
        selected_date=selected_date,
        selected_status=selected_status,
        taken_count=taken_count,
        missed_count=missed_count,
        snoozed_count=snoozed_count,
        total_count=len(history)
    )


# Compatibility alias for old template links
app.add_url_rule(
    "/history",
    endpoint="history",
    view_func=medicine_history,
    methods=["GET"]
)
    
# =========================================================
# CAREGIVER
# =========================================================

@app.route("/caregiver")
@login_required
def caregiver():
    mark_overdue_doses(current_user.id)
    today = date.today().isoformat()

    medicines = Medicine.query.filter_by(
        user_id=current_user.id,
        active=True,
    ).all()

    schedules = (
        MedicineSchedule.query
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            Medicine.active.is_(True),
            MedicineSchedule.active.is_(True),
        )
        .all()
    )

    logs = (
        DoseEvent.query
        .join(MedicineSchedule)
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            DoseEvent.scheduled_date == today,
        )
        .all()
    )

    return render_template(
        "caregiver.html",
        patient=current_user,
        medicines=medicines,
        schedules=schedules,
        logs=logs,
    )


# =========================================================
# MEDICINE ALARM API
# =========================================================

@app.route("/api/medicine-alarms")
@login_required
def medicine_alarms():
    now = datetime.now()
    today = now.date().isoformat()

    schedules = (
        MedicineSchedule.query
        .join(Medicine)
        .filter(
            Medicine.user_id == current_user.id,
            Medicine.active.is_(True),
            MedicineSchedule.active.is_(True),
        )
        .all()
    )

    alarms = []

    for schedule in schedules:
        try:
            scheduled = datetime.strptime(
                schedule.scheduled_time, "%H:%M"
            ).time()
        except (ValueError, TypeError):
            continue

        due_at = datetime.combine(now.date(), scheduled)

        # Trigger only during the 15-minute reminder window.
        if now < due_at or now > due_at + timedelta(minutes=15):
            continue

        event = DoseEvent.query.filter_by(
            schedule_id=schedule.id,
            scheduled_date=today,
        ).first()

        if event and event.status in {"taken", "missed"}:
            continue

        medicine = schedule.medicine

        alarms.append({
            "schedule_id": schedule.id,
            "medicine_id": medicine.id,
            "medicine_name": medicine.name,
            "dosage": medicine.dosage,
            "scheduled_time": schedule.scheduled_time,
            "formatted_time": due_at.strftime("%I:%M %p"),
            "instructions": medicine.instructions or "",
            "status": event.status if event else "pending",
        })

    return jsonify({
        "success": True,
        "current_time": now.strftime("%I:%M %p"),
        "alarms": alarms,
    })


@app.route(
    "/api/medicine-alarms/<int:schedule_id>/response",
    methods=["POST"],
)
@login_required
def medicine_alarm_response(schedule_id):
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()

    if status not in {"taken", "snoozed"}:
        return jsonify({"success": False, "error": "Invalid response."}), 400

    schedule = db.session.get(MedicineSchedule, schedule_id)
    if (
        schedule is None or not schedule.active
        or not schedule.medicine.active
        or schedule.medicine.user_id != current_user.id
    ):
        return jsonify({"success": False, "error": "Medicine schedule not found."}), 404

    today = date.today().isoformat()
    event = DoseEvent.query.filter_by(
        schedule_id=schedule.id, scheduled_date=today
    ).first()

    if event and event.status in {"taken", "missed"}:
        return jsonify({"success": False, "error": "This dose has already been recorded."}), 409

    medicine = schedule.medicine
    if status == "taken" and (medicine.stock_quantity or 0) <= 0:
        return jsonify({"success": False, "error": "Medicine stock is empty. Refill it first."}), 409

    if event is None:
        event = DoseEvent(
            schedule_id=schedule.id, scheduled_date=today,
            status=status, response_time=datetime.now()
        )
        db.session.add(event)
    else:
        event.status = status
        event.response_time = datetime.now()

    if status == "taken":
        medicine.stock_quantity -= 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not save alarm response")
        return jsonify({"success": False, "error": "Could not save response."}), 500

    return jsonify({
        "success": True,
        "status": status,
        "remaining_stock": medicine.stock_quantity,
        "low_stock": medicine.stock_quantity <= 5,
    })


# =========================================================
# AI MEDICATION ADHERENCE RISK PREDICTION
# =========================================================

@app.route("/risk-prediction", methods=["GET"])
@login_required
def risk_prediction():
    today = date.today()
    start_date = today - timedelta(days=30)

    medicines = Medicine.query.filter_by(
        user_id=current_user.id,
        active=True
    ).all()

    total_doses = 0
    taken_doses = 0
    missed_doses = 0
    snoozed_doses = 0

    for medicine in medicines:
        schedules = MedicineSchedule.query.filter_by(
            medicine_id=medicine.id,
            active=True
        ).all()

        for schedule in schedules:
            events = DoseEvent.query.filter(
                DoseEvent.schedule_id == schedule.id,
                DoseEvent.scheduled_date >= start_date.isoformat(),
                DoseEvent.scheduled_date <= today.isoformat()
            ).all()

            for event in events:
                if event.status in ("taken", "missed", "snoozed"):
                    total_doses += 1

                    if event.status == "taken":
                        taken_doses += 1
                    elif event.status == "missed":
                        missed_doses += 1
                    elif event.status == "snoozed":
                        snoozed_doses += 1

    if total_doses == 0:
        adherence = None
        risk_score = None
        risk_level = "Insufficient Data"
        risk_color = "secondary"
        recommendation = (
            "Record your medicine responses to calculate your "
            "medication adherence risk."
        )

    else:
        adherence = round(
            (taken_doses / total_doses) * 100, 1
        )

        missed_rate = missed_doses / total_doses
        snoozed_rate = snoozed_doses / total_doses

        risk_score = min(
            100,
            round((missed_rate * 80 + snoozed_rate * 20))
        )

        if adherence >= 90 and missed_rate < 0.10:
            risk_level = "Low"
            risk_color = "success"
            recommendation = (
                "Your recorded adherence is good. Continue following "
                "your prescribed medicine schedule."
            )

        elif adherence >= 70 and missed_rate < 0.30:
            risk_level = "Medium"
            risk_color = "warning"
            recommendation = (
                "Some doses have been missed or delayed. Keep reminders "
                "enabled and review your medicine schedule."
            )

        else:
            risk_level = "High"
            risk_color = "danger"
            recommendation = (
                "Your records show missed or delayed doses. Consider "
                "stronger reminders and discuss difficulties following "
                "your medication schedule with your healthcare professional."
            )

    return render_template(
        "risk_prediction.html",
        total_doses=total_doses,
        taken_doses=taken_doses,
        missed_doses=missed_doses,
        snoozed_doses=snoozed_doses,
        adherence=adherence,
        risk_score=risk_score,
        risk_level=risk_level,
        risk_color=risk_color,
        recommendation=recommendation
    )
# =========================================================
# PRIVATE MEDICAL RECORDS
# =========================================================

def allowed_medical_record_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_MEDICAL_RECORD_EXTENSIONS
    )


@app.route("/medical-records", methods=["GET", "POST"])
@login_required
def medical_records():

    upload_folder = app.config["MEDICAL_RECORD_UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    if request.method == "POST":

        uploaded_file = request.files.get("document")

        title = request.form.get("title", "").strip()
        record_type = request.form.get("record_type", "").strip().lower()
        doctor_name = request.form.get("doctor_name", "").strip()
        hospital_name = request.form.get("hospital_name", "").strip()

        consultation_date = request.form.get(
            "consultation_date", ""
        ).strip()

        next_consultation_date = request.form.get(
            "next_consultation_date", ""
        ).strip()

        notes = request.form.get("notes", "").strip()

        valid_types = {
            "prescription",
            "blood_report",
            "lab_report",
            "other"
        }

        # Continue with your existing validation and upload code here



        if not uploaded_file or not uploaded_file.filename:
            flash("Please choose a medical document.", "error")
            return redirect(url_for("medical_records"))

        if record_type not in valid_types:
            flash("Please select a valid document type.", "error")
            return redirect(url_for("medical_records"))

        if not consultation_date:
            flash("Please enter the report date.", "error")
            return redirect(url_for("medical_records"))

        try:
            consultation_date = datetime.strptime(
                consultation_date, "%Y-%m-%d"
            ).date().isoformat()

            if next_consultation_date:
                next_consultation_date = datetime.strptime(
                    next_consultation_date, "%Y-%m-%d"
                ).date().isoformat()

        except ValueError:
            flash("Please enter valid dates.", "error")
            return redirect(url_for("medical_records"))

        original_filename = secure_filename(uploaded_file.filename)

        if (
            not original_filename
            or not allowed_medical_record_file(original_filename)
        ):
            flash(
                "Allowed files: PDF, PNG, JPG, JPEG and WEBP.",
                "error"
            )
            return redirect(url_for("medical_records"))

        extension = original_filename.rsplit(".", 1)[1].lower()

        stored_filename = f"{uuid.uuid4().hex}.{extension}"

        file_path = os.path.join(
            upload_folder,
            stored_filename
        )

        try:

            uploaded_file.save(file_path)

            record = MedicalRecord(
                user_id=current_user.id,
                title=(title or original_filename)[:150],
                record_type=record_type,
                doctor_name=doctor_name[:120] or None,
                hospital_name=hospital_name[:150] or None,
                consultation_date=consultation_date,
                next_consultation_date=next_consultation_date or None,
                notes=notes or None,
                original_filename=original_filename[:255],
                stored_filename=stored_filename
            )

            db.session.add(record)
            db.session.commit()

            flash(
                "Medical record uploaded successfully.",
                "success"
            )

            return redirect(url_for("medical_records"))

        except Exception:

            db.session.rollback()

            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    app.logger.exception(
                        "Could not remove failed medical upload"
                    )

            app.logger.exception("Medical record upload failed")

            flash(
                "Could not save the medical record. Please try again.",
                "error"
            )

            return redirect(url_for("medical_records"))

    records = (
        MedicalRecord.query
        .filter_by(user_id=current_user.id)
        .order_by(
            MedicalRecord.consultation_date.desc(),
            MedicalRecord.uploaded_at.desc()
        )
        .all()
    )

    today_string = date.today().isoformat()

    upcoming_records = [
        record for record in records
        if record.next_consultation_date
        and record.next_consultation_date >= today_string
    ]

    upcoming_records.sort(
        key=lambda record: record.next_consultation_date
    )

    return render_template(
        "medical_records.html",
        records=records,
        upcoming_records=upcoming_records,
        today=today_string
    )


@app.route("/medical-records/<int:record_id>/download")
@login_required
def download_medical_record(record_id):

    record = MedicalRecord.query.filter_by(
        id=record_id,
        user_id=current_user.id
    ).first_or_404()

    return send_from_directory(
        app.config["MEDICAL_RECORD_UPLOAD_FOLDER"],
        record.stored_filename,
        as_attachment=True,
        download_name=record.original_filename
    )


@app.route("/medical-records/<int:record_id>/delete", methods=["POST"])
@login_required
def delete_medical_record(record_id):

    record = MedicalRecord.query.filter_by(
        id=record_id,
        user_id=current_user.id
    ).first_or_404()

    file_path = os.path.join(
        app.config["MEDICAL_RECORD_UPLOAD_FOLDER"],
        record.stored_filename
    )

    try:

        db.session.delete(record)
        db.session.commit()

        if os.path.isfile(file_path):
            os.remove(file_path)

        flash("Medical record deleted.", "success")

    except Exception:

        db.session.rollback()

        app.logger.exception("Medical record deletion failed")

        flash("Could not delete this medical record.", "error")

    return redirect(url_for("medical_records"))


@app.errorhandler(413)
def medical_upload_too_large(error):

    flash(
        "File is too large. Maximum upload size is 10 MB.",
        "error"
    )

    if current_user.is_authenticated:
        return redirect(url_for("medical_records"))

    return redirect(url_for("login"))

# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

with app.app_context():
    db.create_all()

    # Add stock_quantity safely to an existing SQLite database.
    try:
        from sqlalchemy import inspect, text
        medicine_columns = {
            column["name"]
            for column in inspect(db.engine).get_columns("medicine")
        }
        if "stock_quantity" not in medicine_columns:
            with db.engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE medicine ADD COLUMN stock_quantity INTEGER NOT NULL DEFAULT 0"
                ))
    except Exception:
        app.logger.exception("Medicine stock migration failed")

    try:
        ensure_schedules_and_migrate_legacy_logs()
    except Exception:
        db.session.rollback()
        app.logger.exception("Schedule/history migration failed")


# =========================================================
# RUN APPLICATION + BACKGROUND SCHEDULER
# =========================================================

# Only run scheduler and debug mode when running via `flask run` or `python app.py`,
# not under gunicorn (which uses multiple workers and would duplicate jobs).
_scheduler = None

def _start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()

    # Send medicine reminders every minute.
    _scheduler.add_job(
        check_and_send_medicine_reminders,
        trigger="interval",
        minutes=1,
        id="medicine_reminder_job",
        max_instances=1,
        coalesce=True,
    )

    # Check for missed doses every minute.
    _scheduler.add_job(
        check_all_users_for_missed_doses,
        trigger="interval",
        minutes=1,
        id="missed_dose_check_job",
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()


if __name__ == "__main__":
    _start_scheduler()
    try:
        app.run(debug=True, use_reloader=False)
    finally:
        if _scheduler:
            _scheduler.shutdown(wait=False)
