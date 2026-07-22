# app/utils/route_helpers.py

import logging
from flask import session
from app.models.models import Users, get_session
from werkzeug.utils import secure_filename
from pathlib import Path
import uuid


logger = logging.getLogger(__name__)
MAX_BATCH_FILES = 20
MAX_PDF_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per PDF
ALLOWED_EXTENSIONS = {'pdf'}
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def allowed_file(filename):
    """
    Check if a file's extension is allowed.
    """
    ALLOWED_EXTENSIONS = {'pdf'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def sanitize_input(input_str):
    invalid_chars = r'<>:"/\|?*'
    return input_str is not None and not any(char in str(input_str) for char in invalid_chars)


def fetch_user_id_from_session():
    username = session.get('username')

    if not username:
        logger.warning("No username found in session.")
        return None

    try:
        with get_session() as db_session:
            user = db_session.query(Users).filter_by(username=username).first()

            if user:
                logger.info(
                    f"Fetched user ID {user.userid} for username {username}. "
                    f"User ID type: {type(user.userid)}"
                )
                return user.userid

            logger.warning(f"No user found for username {username}.")
            return None

    except Exception as e:
        logger.error(f"Error fetching user ID: {str(e)}")
        return None