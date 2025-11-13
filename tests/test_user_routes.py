import unittest
from flask import Flask, url_for, session
from flask_login import LoginManager, current_user
from werkzeug.security import generate_password_hash
from app.models.models import Users
from app.routes.user_routes import user_bp
from app.services.DatabaseConnection import db

class UserRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret-key",
            "LOGIN_DISABLED": False
        })
        db.init_app(self.app)
        # Setup Flask-Login
        login_manager = LoginManager()
        login_manager.init_app(self.app)

        @login_manager.user_loader
        def load_user(user_id):
            return Users.query.get(int(user_id))

        self.app.register_blueprint(user_bp)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()

            # Add a test user
            hashed_pw = generate_password_hash("testpass")
            test_user = Users(username="testuser1", firstname="Test", lastname="User1")
            test_user.password_hash = hashed_pw
            db.session.add(test_user)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_register_new_user(self):
        response = self.client.post("/register", data={
            "username": "newuser1",
            "password": "newpass123",
            "firstname": "New",
            "lastname": "User1"
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"login", response.data)  # Should redirect to login

        # Verify user was created
        with self.app.app_context():
            user = Users.query.filter_by(username="newuser").first()
            self.assertIsNotNone(user)
            self.assertEqual(user.firstname, "New")

    def test_register_duplicate_username(self):
        response = self.client.post("/register", data={
            "username": "testuser",  # Already exists
            "password": "testpass",
            "firstname": "Test",
            "lastname": "User"
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 400)  # Should return 400 error

    def test_login_success(self):
        response = self.client.post("/", data={
            "username": "testuser",
            "password": "testpass"
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"index", response.data)  # Should redirect to index
        self.assertTrue(current_user.is_authenticated)

    def test_login_invalid_credentials(self):
        response = self.client.post("/", data={
            "username": "testuser1",
            "password": "wrongpass"
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)
        self.assertFalse(current_user.is_authenticated)

    def test_logout(self):
        # Login first
        self.client.post("/", data={
            "username": "testuser1",
            "password": "testpass"
        }, follow_redirects=True)

        # Then logout
        response = self.client.get("/logout", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"login", response.data)  # Should redirect to login
        self.assertFalse(current_user.is_authenticated)

    def test_profile_authenticated(self):
        # Login first
        self.client.post("/", data={
            "username": "testuser1",
            "password": "testpass"
        }, follow_redirects=True)

        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test User", response.data)  # Check profile info

    def test_profile_unauthenticated(self):
        response = self.client.get("/profile", follow_redirects=True)
        self.assertIn(b"login", response.data)  # Should redirect to login

    def test_settings_authenticated(self):
        # Login first
        self.client.post("/", data={
            "username": "testuser1",
            "password": "testpass"
        }, follow_redirects=True)

        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)

    def test_settings_unauthenticated(self):
        response = self.client.get("/settings", follow_redirects=True)
        self.assertIn(b"login", response.data)  # Should redirect to login


if __name__ == "__main__":
    unittest.main(verbosity=2)