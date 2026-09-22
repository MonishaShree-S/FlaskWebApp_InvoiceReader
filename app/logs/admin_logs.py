import os
import logging
from flask import current_app

# Configure admin logging
admin_logger = logging.getLogger("admin_actions")
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_actions.log")
handler = logging.FileHandler(_log_path)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
admin_logger.addHandler(handler)
admin_logger.setLevel(logging.INFO)

def log_admin_action(admin, action, target):
    """Logs admin actions like user promotions, deletions, etc."""
    admin_logger.info(f"Admin {admin.username} performed {action} on {target}")
