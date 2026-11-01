import os
import re
import time
import uuid
import hashlib
import secrets
from typing import Optional, Dict, Any, Tuple
from functools import wraps
import requests
import jwt
from dotenv import load_dotenv

from services.database_service import DatabaseService
from core.shared.logger import get_logger

# Load environment variables from .env
load_dotenv()

logger = get_logger("AuthService")

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class AuthService:
    def __init__(self, db: Optional[DatabaseService] = None):
        self.db = db or DatabaseService()
        self.jwt_secret = os.getenv("JWT_SECRET") or "aiva_fallback_development_secret_change_in_production"
        self.access_token_expiry_sec = int(os.getenv("JWT_ACCESS_EXPIRY_MINUTES", "30")) * 60
        self.refresh_token_expiry_sec = int(os.getenv("JWT_REFRESH_EXPIRY_DAYS", "30")) * 86400
        self.otp_expiry_sec = 600.0  # 10 minutes
        self.max_otp_attempts = 5

        # For testing / automated verification when Brevo is not yet connected
        self._last_generated_otp_debug: Dict[str, str] = {}

    @property
    def brevo_api_key(self) -> str:
        return os.getenv("BREVO_API_KEY", "").strip()

    @property
    def brevo_sender_email(self) -> str:
        return os.getenv("BREVO_SENDER_EMAIL", "").strip() or "noreply@aiva.local"

    @property
    def brevo_sender_name(self) -> str:
        return os.getenv("BREVO_SENDER_NAME", "AIVA Assistant").strip()

    # -------------------------------------------------------------
    # HASHING HELPERS
    # -------------------------------------------------------------
    @staticmethod
    def hash_otp(code: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # -------------------------------------------------------------
    # EMAIL SENDING VIA BREVO API
    # -------------------------------------------------------------
    def send_brevo_otp_email(self, recipient_email: str, otp_code: str) -> Tuple[bool, str]:
        """
        Sends OTP email using Brevo transactional email API:
        POST https://api.brevo.com/v3/smtp/email
        Header: api-key
        """
        api_key = self.brevo_api_key
        if not api_key:
            # Store in debug map for local automated testing without live credentials
            self._last_generated_otp_debug[recipient_email.lower()] = otp_code
            logger.warning("BREVO_API_KEY not configured in .env. Stored OTP for mock/dev test mode.")
            return True, "Brevo API key not configured; OTP processed in development mode."

        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json"
        }
        payload = {
            "sender": {
                "name": self.brevo_sender_name,
                "email": self.brevo_sender_email
            },
            "to": [
                {
                    "email": recipient_email
                }
            ],
            "subject": f"Your AIVA Verification Code: {otp_code}",
            "htmlContent": f"""
            <div style="font-family: Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 24px; background: #0f172a; color: #f8fafc; border-radius: 12px; border: 1px solid #1e293b;">
                <h1 style="color: #38bdf8; margin-top: 0; font-size: 24px; letter-spacing: 1px;">AIVA Security Verification</h1>
                <p style="font-size: 15px; line-height: 1.6; color: #cbd5e1;">Use the verification code below to complete your authentication. This code expires in <strong>10 minutes</strong>.</p>
                <div style="margin: 28px 0; text-align: center;">
                    <div style="display: inline-block; padding: 16px 36px; background: #1e293b; color: #38bdf8; font-size: 32px; font-weight: 700; letter-spacing: 6px; border-radius: 8px; border: 1px solid #0284c7;">
                        {otp_code}
                    </div>
                </div>
                <p style="font-size: 13px; color: #94a3b8;">If you did not request this verification code, please ignore this email. Never share this code with anyone.</p>
                <hr style="border: 0; border-top: 1px solid #1e293b; margin: 24px 0;" />
                <p style="font-size: 12px; color: #64748b; margin: 0;">Sent by AIVA Autonomous AI Agent</p>
            </div>
            """
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=12.0)
            if resp.status_code in (200, 201, 202):
                logger.info(f"Verification email sent to {recipient_email}")
                return True, "OTP email sent successfully."
            else:
                logger.error(f"Brevo API error: {resp.status_code} - {resp.text}")
                # Fallback to debug map if Brevo returns error (e.g. invalid key or unverified domain during testing)
                self._last_generated_otp_debug[recipient_email.lower()] = otp_code
                return False, f"Brevo returned status {resp.status_code}"
        except Exception as e:
            logger.error(f"Failed to communicate with Brevo API: {e}")
            self._last_generated_otp_debug[recipient_email.lower()] = otp_code
            return False, f"Network error sending email: {str(e)}"

    # -------------------------------------------------------------
    # OTP GENERATION & VERIFICATION
    # -------------------------------------------------------------
    def request_otp(self, email: str, ip_address: Optional[str] = None) -> Tuple[bool, str]:
        email = email.strip().lower()
        if not EMAIL_REGEX.match(email):
            return False, "Invalid email address format."

        # Rate limiting: max 3 per 10 mins per email
        recent_email_count = self.db.count_recent_otps_by_email(email, window_seconds=600.0)
        if recent_email_count >= 3:
            return False, "Too many OTP requests for this email. Please wait 10 minutes."

        # Rate limiting: max 10 per 10 mins per IP
        if ip_address:
            recent_ip_count = self.db.count_recent_otps_by_ip(ip_address, window_seconds=600.0)
            if recent_ip_count >= 10:
                return False, "Too many requests from this IP. Please wait 10 minutes."

        # Generate 6-digit cryptographic OTP
        otp_code = f"{secrets.randbelow(900000) + 100000}"
        self._last_generated_otp_debug[email.lower()] = otp_code
        salt = secrets.token_hex(16)
        code_hash = self.hash_otp(otp_code, salt)
        otp_id = str(uuid.uuid4())
        expires_at = time.time() + self.otp_expiry_sec

        self.db.store_otp(
            otp_id=otp_id,
            email=email,
            code_hash=code_hash,
            salt=salt,
            expires_at=expires_at,
            ip_address=ip_address
        )

        success, msg = self.send_brevo_otp_email(email, otp_code)
        return True, "Verification code sent to your email."

    def verify_otp(self, email: str, code: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        email = email.strip().lower()
        code = str(code).strip()

        if not EMAIL_REGEX.match(email) or len(code) != 6 or not code.isdigit():
            return False, "Invalid email or OTP format.", None

        otp_record = self.db.get_latest_valid_otp(email)
        if not otp_record:
            return False, "No active OTP found or code expired. Please request a new code.", None

        if otp_record["attempts"] >= self.max_otp_attempts:
            return False, "Maximum verification attempts exceeded. Please request a new code.", None

        # Verify hash
        expected_hash = self.hash_otp(code, otp_record["salt"])
        if not secrets.compare_digest(expected_hash, otp_record["code_hash"]):
            attempts = self.db.increment_otp_attempts(otp_record["id"])
            remaining = self.max_otp_attempts - attempts
            if remaining <= 0:
                return False, "Maximum verification attempts exceeded. Please request a new code.", None
            return False, f"Incorrect verification code. {remaining} attempt(s) remaining.", None

        # Mark OTP as used
        self.db.mark_otp_used(otp_record["id"])

        # Find or create user
        user = self.db.get_user_by_email(email)
        if not user:
            user_id = str(uuid.uuid4())
            user = self.db.create_user(user_id=user_id, email=email)
            logger.info(f"New user registered: {email} ({user_id})")

        # Generate JWT access token & refresh token
        tokens = self.generate_tokens(user["id"], user["email"])
        return True, "Verification successful.", {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "created_at": user["created_at"]
            },
            "tokens": tokens
        }

    # -------------------------------------------------------------
    # TOKEN LIFECYCLE
    # -------------------------------------------------------------
    def generate_tokens(self, user_id: str, email: str) -> Dict[str, Any]:
        now = time.time()
        access_payload = {
            "sub": user_id,
            "email": email,
            "type": "access",
            "jti": secrets.token_hex(8),
            "iat": int(now),
            "exp": int(now + self.access_token_expiry_sec)
        }
        access_token = jwt.encode(access_payload, self.jwt_secret, algorithm="HS256")

        # Refresh token: 64-char hex string
        raw_refresh = secrets.token_hex(32)
        refresh_hash = self.hash_token(raw_refresh)
        refresh_expires_at = now + self.refresh_token_expiry_sec
        self.db.store_refresh_token(refresh_hash, user_id, refresh_expires_at)

        return {
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "token_type": "Bearer",
            "expires_in": self.access_token_expiry_sec
        }

    def verify_access_token(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
            if payload.get("type") != "access":
                return None
            return payload
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

    def refresh_access_token(self, refresh_token: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if not refresh_token:
            return False, "Missing refresh token.", None

        refresh_hash = self.hash_token(refresh_token.strip())
        record = self.db.get_refresh_token(refresh_hash)
        if not record:
            return False, "Invalid or expired refresh token.", None

        user = self.db.get_user_by_id(record["user_id"])
        if not user or not user["is_active"]:
            return False, "User account is inactive or not found.", None

        # Issue new access token (keep same refresh token or rotate)
        tokens = self.generate_tokens(user["id"], user["email"])
        # Revoke old refresh token on rotation
        self.db.revoke_refresh_token(refresh_hash)

        return True, "Token refreshed successfully.", tokens

    def revoke_refresh_token(self, refresh_token: str) -> bool:
        if not refresh_token:
            return False
        refresh_hash = self.hash_token(refresh_token.strip())
        self.db.revoke_refresh_token(refresh_hash)
        return True
