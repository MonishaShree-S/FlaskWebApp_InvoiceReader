import logging
from flask import current_app

# Configure admin logging
admin_logger = logging.getLogger("admin_actions")
handler = logging.FileHandler("admin_actions.log")
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
admin_logger.addHandler(handler)
admin_logger.setLevel(logging.INFO)

def log_admin_action(admin, action, target):
    """Logs admin actions like user promotions, deletions, etc."""
    admin_logger.info(f"Admin {admin.username} performed {action} on {target}")
